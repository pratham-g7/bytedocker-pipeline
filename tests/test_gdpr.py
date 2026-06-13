import json

import pytest
from django.urls import reverse
from django.utils import timezone

from pipeline.gdpr import contact_export_data
from pipeline.models import Activity, Contact, Lead, Task

from .factories import (
    ContactFactory,
    EnrollmentFactory,
    LeadFactory,
    MessageFactory,
    TaskFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


# ---------------------------------------------------------------- export data


def test_export_includes_contact_company_leads_activities():
    contact = ContactFactory(first_name="Ada", title="CTO")
    lead = LeadFactory(contact=contact)
    Activity.objects.create(
        contact=contact, lead=lead, type=Activity.Type.NOTE, payload={"text": "hi"}
    )

    data = contact_export_data(contact)

    assert data["contact"]["first_name"] == "Ada"
    assert data["contact"]["email"] == contact.email
    assert data["company"]["name"] == contact.company.name
    assert len(data["leads"]) == 1
    assert any(a["payload"].get("text") == "hi" for a in data["activities"])


def test_export_includes_enrollments_and_messages():
    enrollment = EnrollmentFactory()
    MessageFactory(enrollment=enrollment, sent_at=timezone.now())
    data = contact_export_data(enrollment.contact)
    assert data["enrollments"][0]["sequence"] == enrollment.sequence.name
    assert len(data["enrollments"][0]["messages"]) == 1


def test_export_is_json_serializable():
    contact = ContactFactory()
    LeadFactory(contact=contact)
    json.dumps(contact_export_data(contact))  # must not raise


def test_export_handles_no_company():
    contact = ContactFactory(company=None)
    assert contact_export_data(contact)["company"] is None


# ---------------------------------------------------------------- export view


def test_export_endpoint_returns_attachment(client, rep):
    contact = ContactFactory(owner=rep)
    client.force_login(rep)
    response = client.get(reverse("contact-export", args=[contact.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert "attachment" in response["Content-Disposition"]
    assert json.loads(response.content)["contact"]["email"] == contact.email


def test_rep_cannot_export_others_contact(client, rep):
    other = ContactFactory()  # owned by someone else
    client.force_login(rep)
    assert client.get(reverse("contact-export", args=[other.pk])).status_code == 404


# ---------------------------------------------------------------- delete view


def test_delete_cascades_cleanly(client, rep):
    contact = ContactFactory(owner=rep)
    lead = LeadFactory(contact=contact)
    Activity.objects.create(contact=contact, type=Activity.Type.NOTE, payload={})
    TaskFactory(lead=lead, owner=rep)
    enrollment = EnrollmentFactory(contact=contact)
    MessageFactory(enrollment=enrollment)
    client.force_login(rep)

    response = client.post(reverse("contact-delete", args=[contact.pk]))

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("contacts")
    assert not Contact.objects.filter(pk=contact.pk).exists()
    assert not Lead.objects.filter(pk=lead.pk).exists()  # cascaded
    assert not Activity.objects.filter(contact_id=contact.pk).exists()
    assert not Task.objects.filter(lead_id=lead.pk).exists()


def test_rep_cannot_delete_others_contact(client, rep):
    other = ContactFactory()
    client.force_login(rep)
    assert client.post(reverse("contact-delete", args=[other.pk])).status_code == 404
    assert Contact.objects.filter(pk=other.pk).exists()
