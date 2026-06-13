import json
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from outreach import tasks
from outreach.models import Enrollment, Mailbox, Message
from outreach.providers.base import ProviderAuthError, TransientProviderError
from outreach.tasks import (
    _fail_send,
    dispatch_due_sends,
    refresh_expiring_tokens,
    reset_daily_counters,
    send_step,
)
from outreach.windows import next_window_open, window_open, within_send_window
from pipeline.models import Activity
from pipeline.models import Task as PipelineTask

from .factories import (
    ContactFactory,
    EnrollmentFactory,
    LeadFactory,
    MailboxFactory,
    SequenceFactory,
    SequenceStepFactory,
)

pytestmark = pytest.mark.django_db

# A Wednesday, noon UTC — deterministic regardless of when the suite runs.
WEDNESDAY_NOON = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
FRIDAY_EVENING = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)  # 19:30 IST


@pytest.fixture(autouse=True)
def _sync_send(monkeypatch):
    """Queue-less tests: dispatch's .delay() runs the real task inline."""
    real = tasks.send_step

    class SyncProxy:
        @staticmethod
        def delay(*args):
            return real(*args)

    monkeypatch.setattr(tasks, "send_step", SyncProxy)


@pytest.fixture
def provider(monkeypatch):
    fake = MagicMock()
    fake.send.return_value = ("pm-1", "th-1")
    monkeypatch.setattr(tasks, "get_provider", lambda mailbox: fake)
    return fake


def _kolkata_mailbox(**kwargs):
    """08:00–18:00 IST window — UTC+5:30, no DST."""
    return MailboxFactory(timezone="Asia/Kolkata", **kwargs)


def _open_mailbox(**kwargs):
    """A mailbox whose window is open at WEDNESDAY_NOON."""
    return MailboxFactory(
        timezone="UTC",
        send_window_start=dtime(0, 0),
        send_window_end=dtime(23, 59, 59),
        **kwargs,
    )


def _due_enrollment(mailbox=None, steps=2, **kwargs):
    sequence = kwargs.pop("sequence", None) or SequenceFactory()
    for order in range(1, steps + 1):
        SequenceStepFactory(sequence=sequence, order=order, wait_days=0 if order == 1 else 3)
    return EnrollmentFactory(
        sequence=sequence,
        mailbox=mailbox or _open_mailbox(),
        next_send_at=WEDNESDAY_NOON - timedelta(minutes=1),
        **kwargs,
    )


# ---------------------------------------------------------------- windows


def test_within_window_respects_mailbox_timezone():
    mailbox = _kolkata_mailbox()
    inside = datetime(2026, 6, 10, 4, 0, tzinfo=UTC)  # 09:30 IST Wed
    before = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)  # 06:30 IST
    after = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)  # 19:30 IST
    assert within_send_window(mailbox, inside)
    assert not within_send_window(mailbox, before)
    assert not within_send_window(mailbox, after)


def test_weekend_blocked_unless_flag_disabled(settings):
    mailbox = _kolkata_mailbox()
    saturday = datetime(2026, 6, 13, 4, 0, tzinfo=UTC)  # 09:30 IST Sat
    assert not within_send_window(mailbox, saturday)
    settings.SEND_WEEKDAYS_ONLY = False
    assert within_send_window(mailbox, saturday)


def test_window_open_returns_now_when_open():
    mailbox = _kolkata_mailbox()
    now = datetime(2026, 6, 10, 4, 0, tzinfo=UTC)
    assert window_open(mailbox, now) == now


def test_window_open_before_start_is_same_day_start():
    mailbox = _kolkata_mailbox()
    now = datetime(2026, 6, 10, 1, 0, tzinfo=UTC)  # 06:30 IST
    assert window_open(mailbox, now) == datetime(2026, 6, 10, 2, 30, tzinfo=UTC)  # 08:00 IST


def test_window_open_after_end_rolls_to_next_day():
    mailbox = _kolkata_mailbox()
    now = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)  # 19:30 IST Wed
    assert window_open(mailbox, now) == datetime(2026, 6, 11, 2, 30, tzinfo=UTC)


def test_window_open_skips_weekend_to_monday():
    mailbox = _kolkata_mailbox()
    opened = window_open(mailbox, FRIDAY_EVENING)
    assert opened == datetime(2026, 6, 15, 2, 30, tzinfo=UTC)  # Monday 08:00 IST


def test_next_window_open_skips_today_even_when_open():
    mailbox = _kolkata_mailbox()
    now = datetime(2026, 6, 10, 4, 0, tzinfo=UTC)  # window currently open
    assert next_window_open(mailbox, now) == datetime(2026, 6, 11, 2, 30, tzinfo=UTC)


# ---------------------------------------------------------------- dispatcher


def test_dispatch_sends_due_step_and_advances(provider):
    enrollment = _due_enrollment()

    queued = dispatch_due_sends(now=WEDNESDAY_NOON)

    assert queued == 1
    provider.send.assert_called_once()
    assert provider.send.call_args.kwargs["to"] == enrollment.contact.email
    message = Message.objects.get()
    assert message.status == Message.Status.SENT
    assert message.provider_message_id == "pm-1"
    enrollment.refresh_from_db()
    assert enrollment.current_step == 1
    # next send ≈ now + 3 days (step 2 wait), within jitter bounds (±90 s)
    delta = enrollment.next_send_at - (timezone.now() + timedelta(days=3))
    assert abs(delta.total_seconds()) <= 91
    enrollment.mailbox.refresh_from_db()
    assert enrollment.mailbox.sends_today == 1
    activity = Activity.objects.get(type=Activity.Type.EMAIL_SENT)
    assert activity.payload["subject"] == message.subject_rendered


def test_last_step_finishes_enrollment(provider):
    enrollment = _due_enrollment(steps=1)
    dispatch_due_sends(now=WEDNESDAY_NOON)
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.FINISHED
    assert enrollment.next_send_at is None


def test_outside_window_defers_to_window_open(provider):
    mailbox = _kolkata_mailbox()  # 12:00 UTC = 17:30 IST → inside; use evening instead
    enrollment = _due_enrollment(mailbox=mailbox)
    evening = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)  # 19:30 IST

    dispatch_due_sends(now=evening)

    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    opens = datetime(2026, 6, 11, 2, 30, tzinfo=UTC)
    assert opens <= enrollment.next_send_at <= opens + timedelta(seconds=91)
    assert enrollment.status == Enrollment.Status.ACTIVE


def test_cap_reached_defers_to_next_day(provider):
    mailbox = _open_mailbox(daily_cap=10, sends_today=10)
    enrollment = _due_enrollment(mailbox=mailbox)

    dispatch_due_sends(now=WEDNESDAY_NOON)

    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    assert enrollment.next_send_at > WEDNESDAY_NOON + timedelta(hours=11)  # next day's window


def test_inactive_mailbox_skipped_without_touching_schedule(provider):
    mailbox = _open_mailbox(status=Mailbox.Status.ERROR)
    enrollment = _due_enrollment(mailbox=mailbox)
    before = enrollment.next_send_at

    dispatch_due_sends(now=WEDNESDAY_NOON)

    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    assert enrollment.next_send_at == before  # retried next tick once reconnected


def test_inactive_sequence_pauses_new_sends(provider):
    sequence = SequenceFactory(is_active=False)
    enrollment = _due_enrollment(sequence=sequence)

    dispatch_due_sends(now=WEDNESDAY_NOON)

    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE  # state kept (DATA_SPEC §3)


def test_unsubscribed_contact_terminalized_at_send_time(provider):
    enrollment = _due_enrollment(contact=ContactFactory(unsubscribed_at=timezone.now()))
    dispatch_due_sends(now=WEDNESDAY_NOON)
    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.UNSUBSCRIBED


def test_bounced_contact_terminalized_at_send_time(provider):
    enrollment = _due_enrollment(contact=ContactFactory(bounced_at=timezone.now()))
    dispatch_due_sends(now=WEDNESDAY_NOON)
    provider.send.assert_not_called()
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.BOUNCED


# ---------------------------------------------------------------- idempotency


def test_send_step_sends_exactly_once(provider):
    enrollment = _due_enrollment()

    send_step(enrollment.pk, 1)
    send_step(enrollment.pk, 1)  # racing duplicate / crashed-worker requeue

    assert provider.send.call_count == 1
    assert Message.objects.count() == 1


def test_two_dispatch_runs_send_once(provider):
    _due_enrollment(steps=2)
    dispatch_due_sends(now=WEDNESDAY_NOON)
    dispatch_due_sends(now=WEDNESDAY_NOON)  # enrollment advanced — no longer due
    assert provider.send.call_count == 1


def test_send_step_skips_paused_enrollment(provider):
    enrollment = _due_enrollment()
    enrollment.pause()
    send_step(enrollment.pk, 1)
    provider.send.assert_not_called()
    assert Message.objects.count() == 0


# ---------------------------------------------------------------- failure modes


def test_retry_config_matches_engine_spec():
    assert send_step.autoretry_for == (TransientProviderError,)
    assert send_step.max_retries == 3
    assert send_step.retry_backoff == 60


def test_exhausted_retries_fail_pause_and_create_task(provider):
    enrollment = _due_enrollment(steps=1)
    lead = LeadFactory(contact=enrollment.contact, owner=enrollment.contact.owner)
    Message.objects.create(  # what send_step leaves behind when every attempt 503s
        enrollment=enrollment,
        step=enrollment.sequence.steps.get(order=1),
        mailbox=enrollment.mailbox,
        status=Message.Status.SCHEDULED,
    )

    send_step.on_failure(
        TransientProviderError("Gmail returned 503"), "tid", (enrollment.pk, 1), {}, None
    )

    message = Message.objects.get()
    assert message.status == Message.Status.FAILED
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.PAUSED  # never silently skip a step
    task = PipelineTask.objects.get(lead=lead)
    assert "investigate" in task.title
    assert Activity.objects.filter(type=Activity.Type.NOTE).exists()


def test_permanent_provider_error_fails_immediately(provider):
    provider.send.side_effect = ValueError("Gmail returned 400")
    enrollment = _due_enrollment()
    LeadFactory(contact=enrollment.contact, owner=enrollment.contact.owner)

    send_step(enrollment.pk, 1)

    assert provider.send.call_count == 1  # no retries for permanent errors
    assert Message.objects.get().status == Message.Status.FAILED
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.PAUSED
    assert PipelineTask.objects.exists()


def test_auth_error_leaves_message_scheduled(provider):
    provider.send.side_effect = ProviderAuthError("revoked")
    enrollment = _due_enrollment()

    send_step(enrollment.pk, 1)

    assert Message.objects.get().status == Message.Status.SCHEDULED
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE  # dispatcher skips error mailbox


def test_failed_message_is_reused_on_resume(provider):
    enrollment = _due_enrollment(steps=1)
    _fail_send(enrollment.pk, 1, reason="setup")
    Message.objects.create(
        enrollment=enrollment,
        step=enrollment.sequence.steps.get(order=1),
        mailbox=enrollment.mailbox,
        status=Message.Status.FAILED,
    )
    enrollment.refresh_from_db()
    enrollment.resume()

    send_step(enrollment.pk, 1)

    assert provider.send.call_count == 1
    message = Message.objects.get()
    assert message.status == Message.Status.SENT  # the failed row flipped, not skipped


def test_empty_rendered_subject_never_reaches_provider(provider):
    enrollment = _due_enrollment(steps=1)
    template = enrollment.sequence.steps.get(order=1).template
    template.subject = "{{title}}"  # contact title is blank → renders empty
    template.save()
    enrollment.contact.title = ""
    enrollment.contact.save()

    send_step(enrollment.pk, 1)

    provider.send.assert_not_called()
    assert Message.objects.get().status == Message.Status.FAILED
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.PAUSED


# ---------------------------------------------------------------- tracking (3.3)


def test_send_injects_open_pixel_and_wrapped_links(provider):
    enrollment = _due_enrollment(steps=1)
    step = enrollment.sequence.steps.get(order=1)
    step.template.body_html = '<p><a href="https://acme.com/pricing">see pricing</a></p>'
    step.template.save()

    send_step(enrollment.pk, 1)

    html = provider.send.call_args.kwargs["html"]
    assert "/t/o/" in html  # open pixel appended
    assert "/t/c/" in html  # link wrapped through the click redirect
    assert 'href="https://acme.com/pricing"' not in html  # original href rewritten


def test_send_includes_unsubscribe_footer_and_header(provider):
    enrollment = _due_enrollment(steps=1)
    send_step(enrollment.pk, 1)

    kwargs = provider.send.call_args.kwargs
    assert "/unsubscribe/" in kwargs["html"]  # footer link
    assert "/unsubscribe/" in kwargs["text"]  # plain-text footer
    headers = kwargs["headers"]
    assert "/unsubscribe/" in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


# ---------------------------------------------------------------- threading (2.8)


def test_step_one_sends_without_thread_ref(provider):
    enrollment = _due_enrollment(steps=2)
    send_step(enrollment.pk, 1)
    assert provider.send.call_args.kwargs["thread_ref"] is None


def test_steps_two_plus_reply_in_step_ones_thread(provider):
    enrollment = _due_enrollment(steps=2)
    send_step(enrollment.pk, 1)
    step_one_subject = Message.objects.get(step__order=1).subject_rendered
    enrollment.refresh_from_db()
    enrollment.next_send_at = timezone.now()  # step 2 due now
    enrollment.save(update_fields=["next_send_at"])

    send_step(enrollment.pk, 2)

    kwargs = provider.send.call_args.kwargs
    assert kwargs["thread_ref"] == {"message_id": "pm-1", "thread_id": "th-1"}
    assert kwargs["subject"] == f"Re: {step_one_subject}"
    assert Message.objects.get(step__order=2).subject_rendered == f"Re: {step_one_subject}"


def test_thread_ref_omitted_when_no_prior_send(provider):
    # step 1 never left (e.g. failed then stopped) — step 2 starts its own thread
    enrollment = _due_enrollment(steps=2)
    enrollment.current_step = 1
    enrollment.save(update_fields=["current_step"])

    send_step(enrollment.pk, 2)

    assert provider.send.call_args.kwargs["thread_ref"] is None


# ---------------------------------------------------------------- scheduled jobs


def test_counters_reset_respects_mailbox_timezone():
    utc_box = MailboxFactory(timezone="UTC", sends_today=5, counters_reset_on=WEDNESDAY_NOON.date())
    # UTC+14: at 12:00 UTC Wednesday it is already Thursday locally
    kiritimati_box = MailboxFactory(
        timezone="Pacific/Kiritimati", sends_today=7, counters_reset_on=WEDNESDAY_NOON.date()
    )

    reset_daily_counters(now=WEDNESDAY_NOON)

    utc_box.refresh_from_db()
    kiritimati_box.refresh_from_db()
    assert utc_box.sends_today == 5  # same local day — untouched
    assert kiritimati_box.sends_today == 0  # local midnight passed — reset


def test_expiring_token_is_refreshed(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(tasks, "get_provider", lambda mailbox: fake)
    soon = MailboxFactory(provider=Mailbox.Provider.OUTLOOK)
    soon.token = json.dumps({"access_token": "a", "expires_at": time.time() + 300})
    soon.save()
    later = MailboxFactory(provider=Mailbox.Provider.OUTLOOK, email="later@outlook.com")
    later.token = json.dumps({"access_token": "b", "expires_at": time.time() + 7200})
    later.save()

    refreshed = refresh_expiring_tokens()

    assert refreshed == 1
    assert fake.refresh_token.call_count == 1
