"""Contact enrichment provider + service (BACKLOG 4.1).

A small provider interface (Apollo today) behind a per-team API key, with a
global APOLLO_API_KEY fallback. enrich_contact() fills only blank fields and
stores the raw provider payload, so enrichment is never destructive.
"""

from typing import Protocol

import requests
from django.conf import settings

from pipeline.models import Activity, Company, Contact, create_open_lead, normalize_domain

TIMEOUT = 20

# Contact fields enrichment may fill (only when currently blank).
FILLABLE = ("first_name", "last_name", "title", "linkedin_url", "phone")


class EnrichmentUnavailable(Exception):
    """No API key configured for the contact's team (or globally)."""


class EnrichmentProvider(Protocol):
    def match(
        self, *, first_name: str, last_name: str, company_name: str, company_domain: str, email: str
    ) -> dict | None:
        """Return normalized fields (+ 'raw' payload) or None if no match."""
        ...


class ApolloProvider:
    """apollo.io people-match enrichment."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def match(self, *, first_name, last_name, company_name, company_domain, email):
        payload = {
            "api_key": self.api_key,
            "first_name": first_name,
            "last_name": last_name,
            "organization_name": company_name,
            "domain": company_domain,
            "email": email or None,
        }
        response = requests.post(
            f"{settings.APOLLO_BASE_URL.rstrip('/')}/v1/people/match",
            json={k: v for k, v in payload.items() if v},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        person = response.json().get("person")
        if not person:
            return None
        org = person.get("organization") or {}
        return {
            "first_name": person.get("first_name", ""),
            "last_name": person.get("last_name", ""),
            "title": person.get("title", ""),
            "email": person.get("email", ""),
            "linkedin_url": person.get("linkedin_url", ""),
            "phone": (person.get("phone_numbers") or [{}])[0].get("raw_number", ""),
            "company_name": org.get("name", ""),
            "company_domain": org.get("primary_domain") or org.get("website_url", ""),
            "raw": person,
        }


def resolve_key(user) -> str:
    """Per-team key first, then the global APOLLO_API_KEY (BACKLOG 4.1)."""
    if user and user.team_id and user.team.enrichment_api_key:
        return user.team.enrichment_api_key
    return settings.APOLLO_API_KEY


def resolve_provider(user) -> EnrichmentProvider | None:
    key = resolve_key(user)
    return ApolloProvider(key) if key else None


def enrich_contact(contact: Contact) -> bool:
    """Fill the contact's blank fields from the provider. Returns whether matched."""
    provider = resolve_provider(contact.owner)
    if provider is None:
        raise EnrichmentUnavailable("No enrichment API key configured.")
    result = provider.match(
        first_name=contact.first_name,
        last_name=contact.last_name,
        company_name=contact.company.name if contact.company else "",
        company_domain=contact.company.domain if contact.company else "",
        email=contact.email,
    )
    if not result:
        return False

    filled = [f for f in FILLABLE if result.get(f) and not getattr(contact, f)]
    for field in filled:
        setattr(contact, field, result[field])
    if contact.company_id is None:
        contact.company = _resolve_company(result)
        if contact.company:
            filled.append("company")
    contact.custom_fields = {**(contact.custom_fields or {}), "enrichment": result.get("raw", {})}
    contact.save()

    summary = ", ".join(filled) or "none"
    Activity.objects.create(
        contact=contact,
        lead=contact.open_lead,
        type=Activity.Type.NOTE,
        payload={"text": f"Enriched via Apollo — filled {len(filled)} field(s): {summary}"},
    )
    return True


def resolve_enrichment_task(task) -> Contact | None:
    """Find an email for a queued (no-email) lead and materialize a Contact (4.2)."""
    provider = resolve_provider(task.owner)
    if provider is None:
        raise EnrichmentUnavailable("No enrichment API key configured.")
    result = provider.match(
        first_name=task.first_name,
        last_name=task.last_name,
        company_name=task.company_name,
        company_domain=task.company_domain,
        email="",
    )
    email = (result or {}).get("email", "").strip().lower()
    if not email:
        return None
    contact = Contact.objects.filter(email=email).first()
    if contact is None:
        contact = Contact.objects.create(
            email=email,
            owner=task.owner,
            source=task.source or "enrichment",
            first_name=result.get("first_name") or task.first_name,
            last_name=result.get("last_name") or task.last_name,
            title=result.get("title") or task.title,
            linkedin_url=result.get("linkedin_url") or task.linkedin_url,
            company=_resolve_company(
                {
                    "company_name": result.get("company_name") or task.company_name,
                    "company_domain": result.get("company_domain") or task.company_domain,
                }
            ),
            custom_fields={"enrichment": result.get("raw", {})},
        )
        create_open_lead(contact)
    return contact


def _resolve_company(result) -> Company | None:
    domain = (result.get("company_domain") or "").strip()
    name = (result.get("company_name") or "").strip()
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
