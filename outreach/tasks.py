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
from django.conf import settings
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from pipeline.models import Activity, Stage
from pipeline.models import Task as PipelineTask

from .models import Enrollment, Mailbox, Message
from .providers import ProviderAuthError, TransientProviderError, get_provider
from .rendering import contact_context, render_string
from .replies import is_auto_reply, is_bounce
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

    # Threading (ENGINE_SPEC §1): steps 2+ reply on the first sent message with the
    # same subject (Re: …) so the sequence reads as one conversation.
    thread_ref = None
    if step_no > 1:
        first = (
            Message.objects.filter(enrollment=enrollment, status=Message.Status.SENT)
            .exclude(provider_message_id="")
            .order_by("step__order")
            .first()
        )
        if first:
            thread_ref = {
                "message_id": first.provider_message_id,
                "thread_id": first.thread_id,
            }
            subject = f"Re: {first.subject_rendered}"

    message.subject_rendered = subject[:300]
    message.save(update_fields=["subject_rendered", "updated_at"])

    try:
        provider_message_id, thread_id = get_provider(mailbox).send(
            to=contact.email, subject=subject, html=html, text=text, thread_ref=thread_ref
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


# ---------------------------------------------------------------- reply polling


@shared_task
def poll_replies():
    """Beat (3 min): fan out one reply-poll task per connected mailbox (ENGINE_SPEC §3)."""
    queued = 0
    mailboxes = Mailbox.objects.filter(status=Mailbox.Status.ACTIVE).exclude(oauth_token="")
    for mailbox_id in mailboxes.values_list("pk", flat=True):
        poll_mailbox_replies.delay(mailbox_id)
        queued += 1
    return queued


@shared_task
def poll_mailbox_replies(mailbox_id):
    """Pull new inbound, match to a sent thread, classify, and handle (ENGINE_SPEC §3).

    The history cursor advances only after the whole batch is handled, so a
    crash re-delivers; reply/bounce handlers are idempotent (timestamp/status
    set once) so replay is safe.
    """
    mailbox = Mailbox.objects.get(pk=mailbox_id)
    try:
        messages, cursor = get_provider(mailbox).fetch_new_messages(mailbox.history_cursor)
    except ProviderAuthError:
        return 0  # provider flagged the mailbox error; owner sees the banner

    handled = 0
    for parsed in messages:
        if not parsed.thread_id or mailbox.email.lower() in (parsed.from_addr or "").lower():
            continue  # our own echo, or no thread to match
        message = (
            Message.objects.filter(thread_id=parsed.thread_id)
            .exclude(thread_id="")
            .select_related("enrollment__contact", "enrollment__sequence")
            .order_by("step__order")
            .first()
        )
        if message is None:
            continue  # not one of our threads
        if is_bounce(parsed):
            _handle_bounce(message, parsed)
        elif is_auto_reply(parsed):
            _log_auto_reply(message, parsed)
        else:
            _handle_reply(message, parsed)
        handled += 1

    mailbox.history_cursor = cursor or ""
    mailbox.save(update_fields=["history_cursor", "updated_at"])
    return handled


def _handle_reply(message, parsed):
    enrollment = message.enrollment
    if message.replied_at is None:  # first-event-wins (DATA_SPEC §3)
        message.replied_at = timezone.now()
        message.save(update_fields=["replied_at", "updated_at"])
    if enrollment.status not in (Enrollment.Status.ACTIVE, Enrollment.Status.PAUSED):
        return  # already terminal — idempotent replay
    enrollment.mark_replied(payload=_inbound_payload(parsed))  # → replied + email_replied Activity
    _advance_lead_stage(enrollment.contact)
    _create_reply_task(enrollment)


def _handle_bounce(message, parsed):
    enrollment = message.enrollment
    if message.status != Message.Status.BOUNCED:
        message.status = Message.Status.BOUNCED
        message.save(update_fields=["status", "updated_at"])
    if enrollment.status not in (Enrollment.Status.ACTIVE, Enrollment.Status.PAUSED):
        return  # already terminal — idempotent replay
    enrollment.mark_bounced(payload=_inbound_payload(parsed))  # → bounced + contact.bounced_at


def _log_auto_reply(message, parsed):
    """Auto-replies don't pause the sequence (ENGINE_SPEC §3) — just note them once."""
    contact = message.enrollment.contact
    if Activity.objects.filter(
        contact=contact, type=Activity.Type.NOTE, payload__auto_reply_id=parsed.provider_message_id
    ).exists():
        return  # idempotent on replay
    Activity.objects.create(
        contact=contact,
        lead=contact.open_lead,
        type=Activity.Type.NOTE,
        payload={
            "text": f"Auto-reply: {parsed.subject}"[:500],
            "auto_reply_id": parsed.provider_message_id,
        },
    )


def _inbound_payload(parsed):
    return {"snippet": (parsed.snippet or "")[:280], "from": parsed.from_addr}


def _advance_lead_stage(contact):
    """Nudge the lead forward on reply (configurable, default on — ENGINE_SPEC §3)."""
    if not getattr(settings, "REPLY_ADVANCES_STAGE", True):
        return
    lead = contact.open_lead
    if lead is None:
        return
    target = Stage.objects.filter(
        name__iexact=getattr(settings, "REPLY_STAGE_NAME", "Engaged")
    ).first()
    if target and lead.stage.order < target.order:  # only advance, never regress
        lead.move_to(target)  # logs stage_change + bumps last_activity_at


def _create_reply_task(enrollment):
    lead = enrollment.contact.open_lead
    if lead is None:
        return  # pipeline.Task is lead-bound
    PipelineTask.objects.create(
        lead=lead,
        owner=lead.owner or enrollment.enrolled_by,
        title=f"Reply from {enrollment.contact} — follow up",
        due_at=timezone.now() + timedelta(days=1),
    )
