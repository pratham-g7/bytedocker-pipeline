import pytest
from django.urls import reverse

from ingestion.models import ImportJob
from ingestion.services import execute_import, guess_mapping
from pipeline.models import Company, Contact

from .factories import CompanyFactory, ContactFactory, UserFactory

pytestmark = pytest.mark.django_db

CSV = """Email,First Name,Last Name,Company,Website,Job Title
jane@acme.com,Jane,Doe,Acme Inc,https://www.acme.com,CTO
bob@beta.io,Bob,,Beta,beta.io,
not-an-email,Carl,Crash,,,
"""

MAPPING = {
    "Email": "email",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Company": "company_name",
    "Website": "company_domain",
    "Job Title": "title",
}


@pytest.fixture
def job(db):
    return ImportJob.objects.create(
        user=UserFactory(role="rep"), filename="leads.csv", raw_csv=CSV, mapping=MAPPING
    )


def test_guess_mapping_from_headers():
    guessed = guess_mapping(["Email", "First Name", "Company", "Website"])
    assert guessed == {
        "Email": "email",
        "First Name": "first_name",
        "Company": "company_name",
        "Website": "company_domain",
    }


def test_import_creates_contacts_companies_and_leads(job):
    execute_import(job)
    assert job.status == ImportJob.Status.DONE
    assert job.created_contacts == 2
    assert job.created_companies == 2
    assert job.errored == 1  # the bad-email row
    assert "invalid email: not-an-email" in job.errors_csv
    assert job.errors_csv.startswith("line,error")

    jane = Contact.objects.get(email="jane@acme.com")
    assert jane.company.domain == "acme.com"
    assert jane.owner == job.user
    assert jane.source == "csv:leads.csv"
    assert jane.open_lead is not None
    assert jane.activities.filter(type="import").exists()


def test_reimport_is_idempotent(job):
    execute_import(job)
    rerun = ImportJob.objects.create(
        user=job.user, filename="leads.csv", raw_csv=CSV, mapping=MAPPING
    )
    execute_import(rerun)
    assert rerun.created_contacts == 0
    assert rerun.updated_contacts == 0
    assert rerun.skipped == 2
    assert Contact.objects.count() == 2
    assert Company.objects.count() == 2


def test_existing_contact_gets_blanks_filled_never_overwritten(job):
    existing = ContactFactory(email="jane@acme.com", first_name="Janet", title="", company=None)
    execute_import(job)
    existing.refresh_from_db()
    assert existing.first_name == "Janet"  # populated field untouched
    assert existing.title == "CTO"  # blank field filled
    assert existing.company.domain == "acme.com"
    assert job.updated_contacts == 1
    assert job.created_contacts == 1  # only bob is new


def test_company_matched_by_domain_not_duplicated(job):
    CompanyFactory(name="ACME Corporation", domain="acme.com")
    execute_import(job)
    assert Company.objects.filter(domain="acme.com").count() == 1
    assert Contact.objects.get(email="jane@acme.com").company.name == "ACME Corporation"


def test_upload_map_run_flow(client, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    user = UserFactory(role="rep")
    client.force_login(user)

    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("flow.csv", CSV.encode(), content_type="text/csv")
    response = client.post(reverse("import-upload"), {"file": upload, "source_label": "apollo"})
    job = ImportJob.objects.get(filename="flow.csv")
    assert response.status_code == 302
    assert job.status == ImportJob.Status.MAPPING

    # mapping page auto-guesses; submit explicit mapping
    response = client.post(
        reverse("import-map", args=[job.pk]),
        {"col_0": "email", "col_1": "first_name", "col_3": "company_name"},
    )
    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == ImportJob.Status.DONE
    assert job.created_contacts == 2
    assert Contact.objects.get(email="jane@acme.com").source == "csv:apollo"


def test_mapping_requires_email_column(client):
    user = UserFactory(role="rep")
    client.force_login(user)
    job = ImportJob.objects.create(user=user, filename="x.csv", raw_csv=CSV)
    response = client.post(reverse("import-map", args=[job.pk]), {"col_1": "first_name"})
    assert response.status_code == 200
    assert b"dedupe key" in response.content


def test_jobs_scoped_to_user(client):
    mine = UserFactory(role="rep")
    theirs = UserFactory(role="rep")
    ImportJob.objects.create(user=theirs, filename="theirs.csv", raw_csv="Email\n")
    client.force_login(mine)
    response = client.get(reverse("imports"))
    assert b"theirs.csv" not in response.content
