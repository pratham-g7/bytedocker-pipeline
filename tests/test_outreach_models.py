from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from outreach.models import Enrollment, InvalidTransition
from pipeline.models import Activity

from .factories import (
    EmailTemplateFactory,
    EnrollmentFactory,
    MailboxFactory,
    SequenceStepFactory,
)

pytestmark = pytest.mark.django_db


# -- Mailbox -----------------------------------------------------------------


def test_mailbox_email_lowercased_at_save():
    mailbox = MailboxFactory(email="Rep@Example.COM ")
    assert mailbox.email == "rep@example.com"


def test_mailbox_token_round_trips_encrypted():
    plaintext = '{"access_token": "abc123", "refresh_token": "def456"}'
    mailbox = MailboxFactory()
    mailbox.token = plaintext
    mailbox.save()

    mailbox.refresh_from_db()
    assert mailbox.token == plaintext
    assert plaintext not in mailbox.oauth_token  # never stored in the clear
    assert "abc123" not in mailbox.oauth_token


def test_mailbox_without_token_reads_empty():
    assert MailboxFactory().token == ""


# -- EmailTemplate -----------------------------------------------------------


def test_body_text_derived_from_html_when_blank():
    template = EmailTemplateFactory(body_html="<p>Hello <b>world</b></p>", body_text="")
    assert "Hello" in template.body_text
    assert "world" in template.body_text
    assert "<p>" not in template.body_text


def test_explicit_body_text_not_overwritten():
    template = EmailTemplateFactory(body_html="<p>HTML version</p>", body_text="text version")
    assert template.body_text == "text version"


def test_unknown_merge_field_rejected_at_save():
    with pytest.raises(ValidationError, match="nickname"):
        EmailTemplateFactory(subject="Hey {{nickname}}")


def test_template_tags_rejected_at_save():
    with pytest.raises(ValidationError):
        EmailTemplateFactory(body_html="{% load static %}<p>hi</p>")


def test_known_fields_and_fallback_accepted():
    template = EmailTemplateFactory(
        subject="For {{company}}",
        body_html="<p>Hi {{first_name|there}}, — {{sender_name}}</p>",
    )
    assert template.pk


# -- SequenceStep ------------------------------------------------------------


def test_step_order_unique_per_sequence():
    step = SequenceStepFactory(order=1)
    with pytest.raises(IntegrityError):
        SequenceStepFactory(sequence=step.sequence, order=1)


# -- Enrollment constraints --------------------------------------------------


@pytest.mark.parametrize("live_status", ["active", "paused"])
def test_double_enrollment_blocked_while_live(live_status):
    enrollment = EnrollmentFactory()
    if live_status == "paused":
        enrollment.pause()
    with pytest.raises(IntegrityError):
        EnrollmentFactory(contact=enrollment.contact, sequence=enrollment.sequence)


def test_reenrollment_allowed_after_terminal_state():
    enrollment = EnrollmentFactory()
    enrollment.mark_finished()
    again = EnrollmentFactory(contact=enrollment.contact, sequence=enrollment.sequence)
    assert again.pk != enrollment.pk


def test_sender_loop_hot_index_exists():
    assert any(
        index.fields == ["status", "next_send_at"] for index in Enrollment._meta.indexes
    ), "dispatcher query needs the (status, next_send_at) index (ENGINE_SPEC §1)"


# -- Enrollment state machine ------------------------------------------------


def test_pause_and_resume_round_trip():
    due = timezone.now() + timedelta(days=2)
    enrollment = EnrollmentFactory(next_send_at=due)
    enrollment.pause()
    assert enrollment.status == Enrollment.Status.PAUSED
    assert enrollment.next_send_at == due  # schedule survives a pause
    enrollment.resume()
    assert enrollment.status == Enrollment.Status.ACTIVE
    assert enrollment.next_send_at == due


def test_pause_requires_active():
    enrollment = EnrollmentFactory()
    enrollment.pause()
    with pytest.raises(InvalidTransition):
        enrollment.pause()


@pytest.mark.parametrize(
    "method", ["mark_replied", "mark_bounced", "mark_unsubscribed", "mark_finished"]
)
def test_terminal_states_never_resume(method):
    enrollment = EnrollmentFactory()
    getattr(enrollment, method)()
    assert enrollment.status in Enrollment.TERMINAL_STATUSES
    assert enrollment.next_send_at is None
    with pytest.raises(InvalidTransition):
        enrollment.resume()
    with pytest.raises(InvalidTransition):
        enrollment.pause()


def test_mark_bounced_sets_contact_bounced_at_and_logs_activity():
    enrollment = EnrollmentFactory()
    assert enrollment.contact.bounced_at is None
    enrollment.mark_bounced(payload={"reason": "550"})

    enrollment.contact.refresh_from_db()
    assert enrollment.contact.bounced_at is not None
    activity = Activity.objects.get(contact=enrollment.contact)
    assert activity.type == Activity.Type.EMAIL_BOUNCED
    assert activity.payload == {"reason": "550"}


def test_mark_bounced_keeps_first_bounce_timestamp():
    first = EnrollmentFactory()
    first.mark_bounced()
    first.contact.refresh_from_db()
    original = first.contact.bounced_at

    second = EnrollmentFactory(contact=first.contact)
    second.mark_bounced()
    first.contact.refresh_from_db()
    assert first.contact.bounced_at == original  # first-event-wins


def test_mark_replied_logs_activity():
    enrollment = EnrollmentFactory()
    enrollment.mark_replied(payload={"snippet": "sounds interesting"})
    activity = Activity.objects.get(contact=enrollment.contact)
    assert activity.type == Activity.Type.EMAIL_REPLIED


def test_mark_unsubscribed_logs_activity():
    enrollment = EnrollmentFactory()
    enrollment.mark_unsubscribed()
    assert Activity.objects.filter(
        contact=enrollment.contact, type=Activity.Type.UNSUBSCRIBED
    ).exists()


def test_terminal_marks_allowed_from_paused():
    enrollment = EnrollmentFactory()
    enrollment.pause()
    enrollment.mark_replied()
    assert enrollment.status == Enrollment.Status.REPLIED


def test_advance_moves_step_and_schedules_next():
    enrollment = EnrollmentFactory()
    next_due = timezone.now() + timedelta(days=3)
    enrollment.advance(1, next_due)
    enrollment.refresh_from_db()
    assert enrollment.current_step == 1
    assert enrollment.next_send_at == next_due
    assert enrollment.status == Enrollment.Status.ACTIVE


def test_advance_requires_active():
    enrollment = EnrollmentFactory()
    enrollment.pause()
    with pytest.raises(InvalidTransition):
        enrollment.advance(1, timezone.now())
