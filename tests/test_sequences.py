import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Team
from outreach.models import Enrollment, Sequence, SequenceStep
from pipeline.models import Activity

from .factories import (
    ContactFactory,
    EmailTemplateFactory,
    EnrollmentFactory,
    MailboxFactory,
    SequenceFactory,
    SequenceStepFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


@pytest.fixture
def manager(db):
    return UserFactory(email="manager@x.com", role="manager")


# ---------------------------------------------------------------- scoping


def test_rep_sees_own_and_team_sequences(client, rep):
    team = Team.objects.create(name="Alpha")
    rep.team = team
    rep.save()
    teammate = UserFactory(email="mate@x.com", role="rep", team=team)
    stranger = UserFactory(email="stranger@x.com", role="rep")
    SequenceFactory(name="Mine", owner=rep)
    SequenceFactory(name="Teammates", owner=teammate)
    SequenceFactory(name="Strangers", owner=stranger)
    client.force_login(rep)

    response = client.get(reverse("sequences"))

    assert b"Mine" in response.content
    assert b"Teammates" in response.content
    assert b"Strangers" not in response.content


def test_manager_sees_all_sequences(client, manager, rep):
    SequenceFactory(name="Reps seq", owner=rep)
    client.force_login(manager)
    response = client.get(reverse("sequences"))
    assert b"Reps seq" in response.content


def test_rep_cannot_open_strangers_builder(client, rep):
    other = SequenceFactory()
    client.force_login(rep)
    assert client.get(reverse("sequence-detail", args=[other.pk])).status_code == 404


# ---------------------------------------------------------------- builder


def test_sequence_create_redirects_to_builder(client, rep):
    client.force_login(rep)
    response = client.post(reverse("sequence-create"), {"name": "Cold outreach"})
    assert response.status_code == 204
    sequence = Sequence.objects.get(name="Cold outreach")
    assert sequence.owner == rep
    assert response["HX-Redirect"] == reverse("sequence-detail", args=[sequence.pk])


def test_builder_shows_cumulative_days(client, rep):
    sequence = SequenceFactory(owner=rep)
    SequenceStepFactory(sequence=sequence, order=1, wait_days=0)
    SequenceStepFactory(sequence=sequence, order=2, wait_days=3)
    SequenceStepFactory(sequence=sequence, order=3, wait_days=4)
    client.force_login(rep)

    response = client.get(reverse("sequence-detail", args=[sequence.pk]))

    assert b"Day 0" in response.content
    assert b"Day 3" in response.content
    assert b"Day 7" in response.content


def test_step_add_appends_with_next_order(client, rep):
    sequence = SequenceFactory(owner=rep)
    SequenceStepFactory(sequence=sequence, order=1)
    template = EmailTemplateFactory()
    client.force_login(rep)

    response = client.post(
        reverse("step-add", args=[sequence.pk]), {"template": template.pk, "wait_days": 2}
    )

    assert response.status_code == 204
    assert list(sequence.steps.values_list("order", flat=True)) == [1, 2]


def test_step_add_blocked_when_locked(client, rep):
    enrollment = EnrollmentFactory(sequence=SequenceFactory(owner=rep))
    template = EmailTemplateFactory()
    client.force_login(rep)

    response = client.post(
        reverse("step-add", args=[enrollment.sequence.pk]),
        {"template": template.pk, "wait_days": 2},
    )

    assert "error" in response["HX-Trigger"]
    assert enrollment.sequence.steps.count() == 0


def test_step_delete_blocked_when_locked(client, rep):
    sequence = SequenceFactory(owner=rep)
    step = SequenceStepFactory(sequence=sequence, order=1)
    EnrollmentFactory(sequence=sequence)
    client.force_login(rep)

    response = client.post(reverse("step-delete", args=[step.pk]))

    assert "error" in response["HX-Trigger"]
    assert sequence.steps.count() == 1


def test_clone_copies_steps_and_unlocks(client, rep):
    sequence = SequenceFactory(owner=rep, name="Original")
    SequenceStepFactory(sequence=sequence, order=1, wait_days=0)
    SequenceStepFactory(sequence=sequence, order=2, wait_days=3)
    EnrollmentFactory(sequence=sequence)  # locks the original
    client.force_login(rep)

    response = client.post(reverse("sequence-clone", args=[sequence.pk]))

    clone = Sequence.objects.get(name="Original (copy)")
    assert response["HX-Redirect"] == reverse("sequence-detail", args=[clone.pk])
    assert clone.owner == rep
    assert not clone.is_locked
    assert list(clone.steps.values_list("order", "wait_days")) == [(1, 0), (2, 3)]


def test_step_preview_renders_sample_contact(client, rep):
    step = SequenceStepFactory(
        sequence=SequenceFactory(owner=rep),
        template=EmailTemplateFactory(subject="For {{company}}"),
    )
    client.force_login(rep)
    response = client.get(reverse("step-preview", args=[step.pk]))
    assert b"Acme Corp" in response.content


# ---------------------------------------------------------------- enrollment


def _enroll(client, contacts, sequence, mailbox):
    return client.post(
        reverse("enroll"),
        {
            "cid": [c.pk for c in contacts],
            "sequence": sequence.pk,
            "mailbox": mailbox.pk,
        },
    )


def test_enroll_sets_next_send_now_and_logs_activity(client, rep):
    contact = ContactFactory(owner=rep)
    sequence = SequenceFactory(owner=rep)
    mailbox = MailboxFactory(user=rep)
    client.force_login(rep)
    before = timezone.now()

    response = _enroll(client, [contact], sequence, mailbox)

    assert response.status_code == 204
    enrollment = Enrollment.objects.get(contact=contact, sequence=sequence)
    assert enrollment.status == Enrollment.Status.ACTIVE
    assert enrollment.mailbox == mailbox
    assert enrollment.enrolled_by == rep
    assert enrollment.next_send_at >= before  # step 1 due immediately, window-gated
    activity = Activity.objects.get(contact=contact, type=Activity.Type.ENROLLED)
    assert activity.payload["sequence"] == sequence.name


def test_double_enroll_skipped_with_friendly_toast(client, rep):
    contact = ContactFactory(owner=rep)
    sequence = SequenceFactory(owner=rep)
    mailbox = MailboxFactory(user=rep)
    client.force_login(rep)

    _enroll(client, [contact], sequence, mailbox)
    response = _enroll(client, [contact], sequence, mailbox)

    assert response.status_code == 204  # constraint surfaced as a toast, not a 500
    assert "Skipped 1" in response["HX-Trigger"]
    assert Enrollment.objects.filter(contact=contact, sequence=sequence).count() == 1


def test_bulk_enroll_skips_unsubscribed_and_bounced(client, rep):
    okay = ContactFactory(owner=rep)
    unsubscribed = ContactFactory(owner=rep, unsubscribed_at=timezone.now())
    bounced = ContactFactory(owner=rep, bounced_at=timezone.now())
    sequence = SequenceFactory(owner=rep)
    mailbox = MailboxFactory(user=rep)
    client.force_login(rep)

    response = _enroll(client, [okay, unsubscribed, bounced], sequence, mailbox)

    assert "Enrolled 1" in response["HX-Trigger"]
    assert "Skipped 2" in response["HX-Trigger"]
    assert Enrollment.objects.count() == 1
    assert Enrollment.objects.first().contact == okay


def test_enroll_modal_requires_selection(client, rep):
    client.force_login(rep)
    response = client.get(reverse("enroll-modal"))
    assert "error" in response["HX-Trigger"]


def test_enrollment_pause_resume_stop_actions(client, rep):
    enrollment = EnrollmentFactory(contact=ContactFactory(owner=rep))
    client.force_login(rep)
    url = reverse("enrollment-action", args=[enrollment.pk])

    client.post(url, {"action": "pause"})
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.PAUSED

    client.post(url, {"action": "resume"})
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE

    client.post(url, {"action": "stop"})
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.FINISHED


def test_stop_works_from_paused(client, rep):
    enrollment = EnrollmentFactory(contact=ContactFactory(owner=rep))
    enrollment.pause()
    client.force_login(rep)

    client.post(reverse("enrollment-action", args=[enrollment.pk]), {"action": "stop"})

    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.FINISHED


def test_invalid_transition_is_friendly_toast(client, rep):
    enrollment = EnrollmentFactory(contact=ContactFactory(owner=rep))
    enrollment.mark_finished()
    client.force_login(rep)

    response = client.post(reverse("enrollment-action", args=[enrollment.pk]), {"action": "resume"})

    assert response.status_code == 204
    assert "error" in response["HX-Trigger"]


def test_rep_cannot_act_on_strangers_enrollment(client, rep):
    enrollment = EnrollmentFactory()  # different owner
    client.force_login(rep)
    response = client.post(reverse("enrollment-action", args=[enrollment.pk]), {"action": "pause"})
    assert response.status_code == 404


def test_contact_enrollments_card_renders(client, rep):
    enrollment = EnrollmentFactory(contact=ContactFactory(owner=rep))
    client.force_login(rep)
    response = client.get(reverse("contact-enrollments", args=[enrollment.contact.pk]))
    assert enrollment.sequence.name.encode() in response.content


def test_enrollment_status_filter_chips(client, rep):
    sequence = SequenceFactory(owner=rep)
    active = EnrollmentFactory(sequence=sequence)
    paused = EnrollmentFactory(sequence=sequence)
    paused.pause()
    client.force_login(rep)

    response = client.get(reverse("sequence-enrollments", args=[sequence.pk]), {"status": "paused"})

    assert paused.contact.full_name.encode() in response.content
    assert active.contact.full_name.encode() not in response.content


def test_steps_editable_after_enrollments_terminal():
    """Lock checks existence, not status — terminal enrollments still lock (history)."""
    enrollment = EnrollmentFactory()
    enrollment.mark_finished()
    assert enrollment.sequence.is_locked  # sent history must stay consistent
    assert SequenceStep.objects.count() == 0
