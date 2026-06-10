import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError

from pipeline.models import Activity, Lead, Stage, create_open_lead, normalize_domain

from .factories import ContactFactory, LeadFactory

pytestmark = pytest.mark.django_db


def test_default_stages_seeded():
    names = list(Stage.objects.values_list("name", flat=True))
    assert names == ["New", "Contacted", "Engaged", "Qualified", "Meeting", "Won", "Lost"]
    assert Stage.objects.get(name="Won").is_won
    assert Stage.objects.get(name="Lost").is_lost


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme.com/about", "acme.com"),
        ("WWW.Example.ORG", "example.org"),
        ("plain.io", "plain.io"),
        ("http://sub.domain.co/path?q=1", "sub.domain.co"),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_contact_email_normalized_and_unique():
    ContactFactory(email="Jane@Acme.COM")
    with pytest.raises(IntegrityError):
        ContactFactory(email="jane@acme.com")


def test_one_open_lead_per_contact():
    lead = LeadFactory()
    with pytest.raises(IntegrityError):
        LeadFactory(contact=lead.contact)


def test_second_lead_allowed_after_first_closes():
    lead = LeadFactory()
    lead.move_to(Stage.objects.get(is_won=True))
    second = create_open_lead(lead.contact)
    assert second.status == Lead.Status.OPEN


def test_move_to_won_sets_status_and_logs_activity():
    lead = LeadFactory()
    lead.move_to(Stage.objects.get(is_won=True), actor=lead.owner)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.WON
    assert lead.last_activity_at is not None
    activity = lead.activities.get(type=Activity.Type.STAGE_CHANGE)
    assert activity.payload == {"from": "New", "to": "Won"}


def test_stage_won_lost_flags_validated():
    stage = Stage(name="Bad", order=99, is_won=True, is_lost=True)
    with pytest.raises(ValidationError):
        stage.full_clean()
    second_won = Stage(name="Also won", order=98, is_won=True)
    with pytest.raises(ValidationError):
        second_won.full_clean()


def test_stage_with_leads_is_delete_protected():
    lead = LeadFactory()
    with pytest.raises(ProtectedError):
        lead.stage.delete()
