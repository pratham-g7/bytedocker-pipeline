"""CSV import engine — dedupe & normalization contract from DATA_SPEC §5.

Rules: email is the primary dedupe key (normalized lowercase); company domain
secondary, exact company-name match as fallback. Existing contacts get blank
fields filled, never overwritten. Re-importing the same file creates nothing.
"""

import csv
import io
from itertools import islice

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from pipeline.models import Activity, Company, Contact, create_open_lead

from .models import ImportJob

# target field -> label shown in the mapping UI
TARGET_FIELDS = {
    "email": "Email (required)",
    "first_name": "First name",
    "last_name": "Last name",
    "title": "Title",
    "phone": "Phone",
    "linkedin_url": "LinkedIn URL",
    "company_name": "Company name",
    "company_domain": "Company domain",
}

CONTACT_FIELDS = ("first_name", "last_name", "title", "phone", "linkedin_url")

# substrings used to auto-guess a mapping from CSV headers (LinkedIn Sales
# Navigator exports use "Profile Url" / "Person Linkedin Url" for the profile).
AUTO_GUESS = [
    ("email", "email"),
    ("first", "first_name"),
    ("last", "last_name"),
    ("title", "title"),
    ("position", "title"),
    ("phone", "phone"),
    ("linkedin", "linkedin_url"),
    ("profile url", "linkedin_url"),
    ("domain", "company_domain"),
    ("website", "company_domain"),
    ("company", "company_name"),
    ("organisation", "company_name"),
    ("organization", "company_name"),
    ("account name", "company_name"),
]


def guess_mapping(headers: list[str]) -> dict:
    mapping, used = {}, set()
    for header in headers:
        lowered = header.lower()
        for needle, target in AUTO_GUESS:
            if needle in lowered and target not in used:
                mapping[header] = target
                used.add(target)
                break
    return mapping


def parse_preview(raw_csv: str, rows: int = 5) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(raw_csv))
    try:
        headers = next(reader)
    except StopIteration:
        return [], []
    return headers, list(islice(reader, rows))


def execute_import(job: ImportJob) -> None:
    job.status = ImportJob.Status.RUNNING
    job.save(update_fields=["status", "updated_at"])

    errors: list[tuple[int, str]] = []
    reader = csv.DictReader(io.StringIO(job.raw_csv))
    for line_no, row in enumerate(reader, start=2):
        try:
            with transaction.atomic():
                _import_row(job, row)
        except Exception as exc:  # one bad row never kills the run
            job.errored += 1
            errors.append((line_no, str(exc)))

    if errors:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["line", "error"])
        writer.writerows(errors)
        job.errors_csv = out.getvalue()

    job.status = ImportJob.Status.DONE
    job.save()


def _value(job: ImportJob, row: dict, target: str) -> str:
    for column, mapped_target in job.mapping.items():
        if mapped_target == target:
            return (row.get(column) or "").strip()
    return ""


def _import_row(job: ImportJob, row: dict) -> None:
    email = _value(job, row, "email").lower()
    if not email:
        _queue_for_enrichment(job, row)  # LinkedIn-style rows with no email (BACKLOG 4.2)
        return
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError(f"invalid email: {email}") from None

    company = _resolve_company(job, row)

    contact = Contact.objects.filter(email=email).first()
    if contact is None:
        contact = Contact.objects.create(
            email=email,
            company=company,
            owner=job.user,
            source=job.source,
            **{field: _value(job, row, field) for field in CONTACT_FIELDS},
        )
        create_open_lead(contact)
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.IMPORT,
            actor=job.user,
            payload={"source": job.source},
        )
        job.created_contacts += 1
        return

    # existing contact: fill blanks only (DATA_SPEC §5.1)
    changed = []
    for field in CONTACT_FIELDS:
        incoming = _value(job, row, field)
        if incoming and not getattr(contact, field):
            setattr(contact, field, incoming)
            changed.append(field)
    if company and contact.company_id is None:
        contact.company = company
        changed.append("company")
    if changed:
        contact.save()
        job.updated_contacts += 1
    else:
        job.skipped += 1


def _queue_for_enrichment(job: ImportJob, row: dict) -> None:
    from .models import EnrichmentTask

    EnrichmentTask.objects.create(
        owner=job.user,
        source=job.source,
        first_name=_value(job, row, "first_name"),
        last_name=_value(job, row, "last_name"),
        title=_value(job, row, "title"),
        company_name=_value(job, row, "company_name"),
        company_domain=_value(job, row, "company_domain"),
        linkedin_url=_value(job, row, "linkedin_url"),
    )
    job.queued += 1


def _resolve_company(job: ImportJob, row: dict) -> Company | None:
    from pipeline.models import normalize_domain

    domain = _value(job, row, "company_domain")
    name = _value(job, row, "company_name")
    if domain:
        domain = normalize_domain(domain)
        company = Company.objects.filter(domain=domain).first()
        if company:
            return company
        company = Company.objects.create(name=name or domain, domain=domain)
        job.created_companies += 1
        return company
    if name:
        company = Company.objects.filter(name__iexact=name).first()
        if company:
            return company
        company = Company.objects.create(name=name)
        job.created_companies += 1
        return company
    return None
