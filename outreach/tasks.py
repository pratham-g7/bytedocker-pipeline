"""The sender loop (ENGINE_SPEC §1) + scheduled jobs (§5).

dispatch_due_sends runs every 60 s off beat, walks due enrollments under
select_for_update(skip_locked) and applies the guard ladder; the actual send is
send_step, its own task with an idempotency guard, autoretry on transient
provider errors, and the §6 failure contract (fail message → pause → rep Task).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from celery import Task as CeleryTask
from celery import shared_task
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from pipeline.models import Activity
from pipeline.models import Task as PipelineTask

from .models import Enrollment, Mailbox, Message
from .providers import ProviderAuthError, TransientProviderError, get_provider
from .rendering import contact_context, render_string
from .windows import jitter, next_window_open, positive_jitter, window_open, within_send_window

logger = logging.getLogger(__name__)

BATCH = 50


# ---------------------------------------------------------------- dispatcher


def _due_enrollments(now):
    qs = (
        Enrollment.objects.filter(status=Enrollment.Status.ACTIVE, next_send_at__lte=now)
        .select_related("mailbox", "contact", "sequence")
        .order_by("next_send_at")
    )
    if connection.features.has_select_for_update_skip_locked:
        qs = qs.select_for_update(skip_locked=True, of=("self",))  # no double-send across workers
    return qs[:BATCH]


@shared_task
def dispatch_due_sends(now=None):
    now = now or timezone.now()
    queued = 0
    with transaction.atomic():
        for enrollment in _due_enrollments(now):
            mailbox = enrollment.mailbox
            if mailbox.status != Mailbox.Status.ACTIVE:
                continue  # leave next_send_at — retried next tick once reconnected
            if not enrollment.sequence.is_active:
                continue  # deactivated sequence pauses new sends, keeps state (DATA_SPEC §3)
            if not within_send_window(mailbox, now):
                enrollment.next_send_at = window_open(mailbox, now) + positive_jitter()
                enrollment.save(update_fields=["next_send_at", "updated_at"])
                continue
            if mailbox.sends_today >= mailbox.daily_cap:
                enrollment.next_send_at = next_window_open(mailbox, now) + positive_jitter()
                enrollment.save(update_fields=["next_send_at", "updated_at"])
                continue
            if enrollment.contact.unsubscribed_at:  # last-moment suppression (ENGINE_SPEC §6)
                enrollment.mark_unsubscribed()
                continue
            if enrollment.contact.bounced_at:
                enrollment.mark_bounced()
                continue
            send_step.delay(enrollment.pk, enrollment.current_step + 1)
            queued += 1
    return queued


# ---------------------------------------------------------------- send_step


class SendStepTask(CeleryTask):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # autoretry exhausted (max 3) — never silently skip a step (ENGINE_SPEC §1)
        _fail_send(args[0], args[1], reason=str(exc))


@shared_task(
    bind=True,
    base=SendStepTask,
    autoretry_for=(TransientProviderError,),
    retry_backoff=60,  # 60 s → 120 s → 240 s
    retry_jitter=True,
    max_retries=3,
)
def send_step(self, enrollment_id, step_no):
    enrollment = Enrollment.objects.select_related(
        "contact__company", "contact__owner", "mailbox", "sequence"
    ).get(pk=enrollment_id)
    if enrollment.status != Enrollment.Status.ACTIVE:
        return  # paused/terminal since dispatch
    step = enrollment.sequence.steps.select_related("template").filter(order=step_no).first()
    if step is None:  # sequence emptied while unenrolled state changed — nothing to send
        enrollment.mark_finished()
        return

    with transaction.atomic():
        # Idempotency guard first: if the send already left, never repeat it.
        # A scheduled/failed row is a retry leftover — reuse it, don't skip the step.
        if Message.objects.filter(
            enrollment=enrollment,
            step__order=step_no,
            status__in=[Message.Status.SENT, Message.Status.BOUNCED],
        ).exists():
            return
        message = Message.objects.filter(
            enrollment=enrollment, step__order=step_no
        ).first() or Message.objects.create(
            enrollment=enrollment, step=step, mailbox=enrollment.mailbox
        )

    contact, mailbox = enrollment.contact, enrollment.mailbox
    context = contact_context(contact, mailbox)
    subject = render_string(step.template.subject, context)
    html = render_string(step.template.body_html, context, autoescape=True)
    text = render_string(step.template.body_text or step.template.body_html, context)
    # [Phase 3 seam: wrap_links(html, message.uuid) + open pixel + unsubscribe footer]
    if not subject.strip() or not html.strip():
        _fail_send(enrollment_id, step_no, reason="template rendered an empty subject/body")
        return  # a broken render must never reach a send (ENGINE_SPEC §6)
    message.subject_rendered = subject[:300]
    message.save(update_fields=["subject_rendered", "updated_at"])

    try:
        # thread_ref for steps 2+ lands with task 2.8 (threading)
        provider_message_id, thread_id = get_provider(mailbox).send(
            to=contact.email, subject=subject, html=html, text=text
        )
    except TransientProviderError:
        raise  # autoretry ×3 with backoff; on_failure then applies the §6 contract
    except ProviderAuthError:
        return  # mailbox already flagged error by the provider; dispatcher skips it now
    except Exception as exc:  # permanent provider error (4xx) — no point retrying
        _fail_send(enrollment_id, step_no, reason=str(exc))
        return

    message.provider_message_id = provider_message_id
    message.thread_id = thread_id or ""
    message.status = Message.Status.SENT
    message.sent_at = timezone.now()
    message.save(
        update_fields=["provider_message_id", "thread_id", "status", "sent_at", "updated_at"]
    )
    Mailbox.objects.filter(pk=mailbox.pk).update(sends_today=F("sends_today") + 1)
    Activity.objects.create(
        contact=contact,
        lead=contact.open_lead,
        type=Activity.Type.EMAIL_SENT,
        payload={
            "message_id": str(message.uuid),
            "subject": subject,
            "sequence": enrollment.sequence.name,
            "step": step_no,
        },
    )
    next_step = enrollment.sequence.steps.filter(order=step_no + 1).first()
    if next_step is None:
        enrollment.mark_finished()
    else:
        enrollment.advance(step_no, timezone.now() + timedelta(days=next_step.wait_days) + jitter())


def _fail_send(enrollment_id, step_no, reason=""):
    """ENGINE_SPEC §6: fail the message, pause the enrollment, raise a rep Task."""
    enrollment = Enrollment.objects.select_related("contact").get(pk=enrollment_id)
    Message.objects.filter(
        enrollment=enrollment, step__order=step_no, status=Message.Status.SCHEDULED
    ).update(status=Message.Status.FAILED)
    if enrollment.status == Enrollment.Status.ACTIVE:
        enrollment.pause()
    lead = enrollment.contact.open_lead
    if lead:  # pipeline.Task is lead-bound; the pause + note still surface it without one
        PipelineTask.objects.create(
            lead=lead,
            owner=lead.owner or enrollment.enrolled_by,
            title=f"Send failed for {enrollment.contact} — investigate",
            due_at=timezone.now(),
        )
    Activity.objects.create(
        contact=enrollment.contact,
        lead=lead,
        type=Activity.Type.NOTE,
        payload={"text": f"Sequence send failed (step {step_no}): {reason}"[:500]},
    )
    logger.warning("send_step failed for enrollment %s step %s: %s", enrollment_id, step_no, reason)


# ---------------------------------------------------------------- scheduled jobs


@shared_task
def reset_daily_counters(now=None):
    """Hourly: zero sends_today once per mailbox-local day (ENGINE_SPEC §5)."""
    now = now or timezone.now()
    reset = 0
    for mailbox in Mailbox.objects.all():
        local_date = now.astimezone(ZoneInfo(mailbox.timezone or "UTC")).date()
        if mailbox.counters_reset_on != local_date:
            mailbox.sends_today = 0
            mailbox.counters_reset_on = local_date
            mailbox.save(update_fields=["sends_today", "counters_reset_on", "updated_at"])
            reset += 1
    return reset


def _token_expiry(mailbox):
    """Expiry from either provider's token JSON — no SDK imports needed."""
    data = json.loads(mailbox.token)
    if "expires_at" in data:  # graph: epoch seconds
        return datetime.fromtimestamp(data["expires_at"], tz=UTC)
    if data.get("expiry"):  # google authorized-user JSON: ISO 8601
        return datetime.fromisoformat(data["expiry"].replace("Z", "+00:00"))
    return None


@shared_task
def refresh_expiring_tokens(now=None):
    """Every 30 min: proactively refresh tokens < 10 min from expiry (ENGINE_SPEC §5)."""
    now = now or timezone.now()
    refreshed = 0
    for mailbox in Mailbox.objects.filter(status=Mailbox.Status.ACTIVE).exclude(oauth_token=""):
        try:
            expiry = _token_expiry(mailbox)
            if expiry is None or expiry - now > timedelta(minutes=10):
                continue
            get_provider(mailbox).refresh_token()
            refreshed += 1
        except ProviderAuthError:
            continue  # provider already flagged the mailbox; owner sees the banner
        except Exception:
            logger.exception("token refresh failed for mailbox %s", mailbox.pk)
    return refreshed
