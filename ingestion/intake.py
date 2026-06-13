"""Webhook / hosted-form lead intake (PLAN §5, BACKLOG 3.5).

Shares the DATA_SPEC §5 dedupe contract with the CSV importer: email is the
primary key (fill blanks, never overwrite), company resolved by domain then
name. Optionally auto-enrolls the new contact into the source's sequence.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from outreach.models import Enrollment, Mailbox
from pipeline.models import Activity, Company, Contact, create_open_lead, normalize_domain

CONTACT_FIELDS = ("first_name", "last_name", "title", "phone", "linkedin_url")


def intake_contact(source, channel: str, data: dict) -> tuple[Contact, bool]:
    """Create/update a Contact from captured `data`. Returns (contact, created)."""
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise ValueError("missing email")
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError(f"invalid email: {email}") from None

    with transaction.atomic():
        company = _resolve_company(data)
        contact = Contact.objects.filter(email=email).first()
        created = contact is None
        if created:
            contact = Contact.objects.create(
                email=email,
                company=company,
                owner=source.owner,
                source=source.contact_source(channel),
                **{f: (data.get(f) or "").strip() for f in CONTACT_FIELDS},
            )
            create_open_lead(contact)
        else:
            _fill_blanks(contact, company, data)
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.NOTE,
            payload={"text": f"Captured via {channel}: {source.name}"},
        )

    _maybe_auto_enroll(source, contact)
    return contact, created


def _fill_blanks(contact, company, data):
    changed = []
    for field in CONTACT_FIELDS:
        incoming = (data.get(field) or "").strip()
        if incoming and not getattr(contact, field):
            setattr(contact, field, incoming)
            changed.append(field)
    if company and contact.company_id is None:
        contact.company = company
        changed.append("company")
    if changed:
        contact.save()


def _resolve_company(data) -> Company | None:
    domain = (data.get("company_domain") or data.get("domain") or "").strip()
    name = (data.get("company_name") or data.get("company") or "").strip()
    if domain:
        domain = normalize_domain(domain)
        return Company.objects.filter(domain=domain).first() or Company.objects.create(
            name=name or domain, domain=domain
        )
    if name:
        return Company.objects.filter(name__iexact=name).first() or Company.objects.create(
            name=name
        )
    return None


def _maybe_auto_enroll(source, contact):
    """Enroll into the source's sequence when configured and the contact is mailable."""
    if not source.auto_enroll_id or source.mailbox_id is None:
        return
    if contact.unsubscribed_at or contact.bounced_at:
        return
    if source.mailbox.status != Mailbox.Status.ACTIVE:
        return
    try:
        with transaction.atomic():  # ride the partial-unique constraint (no double-enroll)
            enrollment = Enrollment.objects.create(
                contact=contact,
                sequence=source.auto_enroll,
                mailbox=source.mailbox,
                enrolled_by=source.owner,
                next_send_at=timezone.now(),
            )
    except Exception:
        return  # already live-enrolled in this sequence
    Activity.objects.create(
        contact=contact,
        lead=contact.open_lead,
        type=Activity.Type.ENROLLED,
        payload={"sequence": source.auto_enroll.name, "enrollment_id": enrollment.pk},
    )
