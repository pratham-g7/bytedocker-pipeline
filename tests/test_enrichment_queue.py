from unittest.mock import MagicMock

import pytest
from django.urls import reverse

from ingestion import enrichment
from ingestion.models import EnrichmentTask, ImportJob
from ingestion.services import execute_import, guess_mapping
from pipeline.models import Contact

from .factories import UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- LinkedIn preset


def test_guess_mapping_handles_linkedin_headers():
    guessed = guess_mapping(
        ["First Name", "Last Name", "Title", "Company", "Profile Url", "Account Name"]
    )
    assert guessed["First Name"] == "first_name"
    assert guessed["Title"] == "title"
    assert guessed["Profile Url"] == "linkedin_url"
    # Company is matched first; Account Name is a redundant company alias
    assert guessed["Company"] == "company_name"


# ---------------------------------------------------------------- import → queue


LINKEDIN_CSV = """First Name,Last Name,Title,Company,Profile Url
Ada,Lovelace,CTO,Acme,https://linkedin.com/in/ada
Grace,Hopper,VP Eng,Beta,https://linkedin.com/in/grace
"""

MAPPING = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Title": "title",
    "Company": "company_name",
    "Profile Url": "linkedin_url",
}


def test_missing_email_rows_are_queued():
    job = ImportJob.objects.create(
        user=UserFactory(role="rep"), filename="linkedin.csv", raw_csv=LINKEDIN_CSV, mapping=MAPPING
    )
    execute_import(job)

    assert job.queued == 2
    assert job.created_contacts == 0
    assert job.errored == 0  # no email is not an error, it's a queue
    queued = EnrichmentTask.objects.filter(owner=job.user)
    assert queued.count() == 2
    ada = queued.get(first_name="Ada")
    assert ada.company_name == "Acme"
    assert ada.linkedin_url.endswith("/ada")
    assert ada.status == EnrichmentTask.Status.PENDING


# ---------------------------------------------------------------- resolve / dismiss


@pytest.fixture
def apollo(monkeypatch):
    def configure(payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        monkeypatch.setattr(enrichment.requests, "post", lambda *a, **k: response)

    return configure


def _task(owner):
    return EnrichmentTask.objects.create(
        owner=owner, first_name="Ada", last_name="Lovelace", company_name="Acme"
    )


def test_resolve_creates_contact(client, apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo(
        {
            "person": {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "title": "CTO",
                "email": "ada@acme.com",
                "organization": {"name": "Acme", "primary_domain": "acme.com"},
            }
        }
    )
    rep = UserFactory(email="rep@x.com", role="rep")
    task = _task(rep)
    client.force_login(rep)

    response = client.post(reverse("enrichment-resolve", args=[task.pk]))

    assert response.status_code == 204
    task.refresh_from_db()
    assert task.status == EnrichmentTask.Status.RESOLVED
    contact = Contact.objects.get(email="ada@acme.com")
    assert contact.title == "CTO"
    assert contact.open_lead is not None


def test_resolve_no_email_keeps_pending(client, apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo({"person": {"first_name": "Ada", "email": ""}})
    rep = UserFactory(email="rep@x.com", role="rep")
    task = _task(rep)
    client.force_login(rep)

    response = client.post(reverse("enrichment-resolve", args=[task.pk]))

    assert "error" in response["HX-Trigger"]
    task.refresh_from_db()
    assert task.status == EnrichmentTask.Status.PENDING


def test_dismiss(client):
    rep = UserFactory(email="rep@x.com", role="rep")
    task = _task(rep)
    client.force_login(rep)
    client.post(reverse("enrichment-dismiss", args=[task.pk]))
    task.refresh_from_db()
    assert task.status == EnrichmentTask.Status.DISMISSED


def test_queue_scoped_to_owner(client):
    rep = UserFactory(email="rep@x.com", role="rep")
    _task(rep)
    _task(UserFactory(email="other@x.com", role="rep"))
    client.force_login(rep)
    response = client.get(reverse("enrichment-queue"))
    assert list(response.context["tasks"]) == list(EnrichmentTask.objects.filter(owner=rep))
