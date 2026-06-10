import pytest
from django.urls import reverse
from django.utils import timezone

from pipeline.models import Activity, Contact, Lead, Stage, Task

from .factories import ContactFactory, LeadFactory, TaskFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


@pytest.fixture
def manager(db):
    return UserFactory(email="manager@x.com", role="manager")


@pytest.fixture
def admin(db):
    return UserFactory(email="admin@x.com", role="admin")


# ---------------------------------------------------------------- contacts


def test_rep_sees_only_own_contacts(client, rep, manager):
    ContactFactory(owner=rep)
    ContactFactory(owner=manager)
    client.force_login(rep)
    response = client.get(reverse("contacts"))
    assert response.context["page"].paginator.count == 1


def test_manager_sees_all_contacts(client, rep, manager):
    ContactFactory(owner=rep)
    ContactFactory(owner=manager)
    client.force_login(manager)
    response = client.get(reverse("contacts"))
    assert response.context["page"].paginator.count == 2


def test_contact_search_filters(client, manager):
    ContactFactory(first_name="Alice", last_name="Zed", owner=manager)
    ContactFactory(first_name="Bob", last_name="Yim", owner=manager)
    client.force_login(manager)
    response = client.get(reverse("contacts"), {"q": "alice"})
    assert response.context["page"].paginator.count == 1


def test_contact_create_defaults_owner_to_creator(client, rep):
    client.force_login(rep)
    response = client.post(reverse("contact-create"), {"email": "NEW@Example.com"})
    assert response.status_code == 204
    contact = Contact.objects.get(email="new@example.com")
    assert contact.owner == rep


def test_rep_cannot_open_foreign_contact(client, rep, manager):
    other = ContactFactory(owner=manager)
    client.force_login(rep)
    assert client.get(reverse("contact-detail", args=[other.pk])).status_code == 404


def test_note_add_creates_activity(client, rep):
    contact = ContactFactory(owner=rep)
    client.force_login(rep)
    response = client.post(reverse("contact-note-add", args=[contact.pk]), {"text": "called him"})
    assert response.status_code == 204
    note = contact.activities.get(type=Activity.Type.NOTE)
    assert note.payload == {"text": "called him"}
    assert note.actor == rep


def test_lead_create_via_contact(client, rep):
    contact = ContactFactory(owner=rep)
    client.force_login(rep)
    response = client.post(reverse("contact-lead-create", args=[contact.pk]))
    assert response.status_code == 204
    assert contact.open_lead is not None
    # second attempt is rejected
    response = client.post(reverse("contact-lead-create", args=[contact.pk]))
    assert b"error" in response.headers["HX-Trigger"].encode()


# ---------------------------------------------------------------- board & leads


def test_board_renders_all_stage_columns(client, manager):
    LeadFactory(contact__owner=manager, owner=manager)
    client.force_login(manager)
    response = client.get(reverse("board"))
    assert response.status_code == 200
    assert len(response.context["columns"]) == Stage.objects.count()


def test_lead_move_writes_activity_and_status(client, rep):
    lead = LeadFactory(contact__owner=rep, owner=rep)
    won = Stage.objects.get(is_won=True)
    client.force_login(rep)
    response = client.post(reverse("lead-move", args=[lead.pk]), {"stage": won.pk})
    assert response.status_code == 204
    lead.refresh_from_db()
    assert lead.status == Lead.Status.WON
    assert lead.activities.filter(type=Activity.Type.STAGE_CHANGE).exists()


def test_rep_cannot_move_foreign_lead(client, rep, manager):
    lead = LeadFactory(contact__owner=manager, owner=manager)
    stage = Stage.objects.get(name="Contacted")
    client.force_login(rep)
    response = client.post(reverse("lead-move", args=[lead.pk]), {"stage": stage.pk})
    assert response.status_code == 404


def test_leads_list_stale_filter(client, manager):
    fresh = LeadFactory(contact__owner=manager, owner=manager)
    fresh.last_activity_at = timezone.now()
    fresh.save()
    stale = LeadFactory(contact__owner=manager, owner=manager)
    stale.last_activity_at = timezone.now() - timezone.timedelta(days=10)
    stale.save()
    client.force_login(manager)
    response = client.get(reverse("leads"), {"stale": "7"})
    assert [lead.pk for lead in response.context["page"]] == [stale.pk]


# ---------------------------------------------------------------- tasks


def test_task_toggle_done_logs_activity(client, rep):
    task = TaskFactory(lead__contact__owner=rep, lead__owner=rep, owner=rep)
    client.force_login(rep)
    response = client.post(reverse("task-toggle", args=[task.pk]))
    assert response.status_code == 204
    task.refresh_from_db()
    assert task.done_at is not None
    assert task.lead.activities.filter(type=Activity.Type.TASK_DONE).exists()


def test_tasks_split_overdue_and_upcoming(client, rep):
    overdue = TaskFactory(
        lead__contact__owner=rep,
        lead__owner=rep,
        owner=rep,
        due_at=timezone.now() - timezone.timedelta(days=1),
    )
    client.force_login(rep)
    response = client.get(reverse("tasks"))
    assert overdue in response.context["overdue"]


# ---------------------------------------------------------------- stage settings


def test_stage_settings_requires_admin(client, rep, admin):
    client.force_login(rep)
    assert client.get(reverse("stages-settings")).status_code == 403
    client.force_login(admin)
    assert client.get(reverse("stages-settings")).status_code == 200


def test_stage_add_and_reorder(client, admin):
    client.force_login(admin)
    client.post(reverse("stage-add"), {"name": "Negotiation"})
    stage = Stage.objects.get(name="Negotiation")
    assert stage.order == 8
    before = stage.order
    client.post(reverse("stage-move", args=[stage.pk]), {"dir": "up"})
    stage.refresh_from_db()
    assert stage.order < before


def test_stage_delete_blocked_when_leads_exist(client, admin):
    lead = LeadFactory()
    client.force_login(admin)
    response = client.post(reverse("stage-delete", args=[lead.stage.pk]))
    assert Stage.objects.filter(pk=lead.stage.pk).exists()
    assert "error" in response.headers["HX-Trigger"]


def test_stage_delete_empty_stage(client, admin):
    client.force_login(admin)
    client.post(reverse("stage-add"), {"name": "Temp"})
    stage = Stage.objects.get(name="Temp")
    client.post(reverse("stage-delete", args=[stage.pk]))
    assert not Stage.objects.filter(pk=stage.pk).exists()


def test_task_create_from_contact(client, rep):
    contact = ContactFactory(owner=rep)
    client.force_login(rep)
    client.post(reverse("contact-lead-create", args=[contact.pk]))
    response = client.post(
        reverse("contact-task-create", args=[contact.pk]),
        {"title": "Follow up", "due_at": "2026-06-15T10:00"},
    )
    assert response.status_code == 204
    assert Task.objects.filter(lead__contact=contact, title="Follow up").exists()
