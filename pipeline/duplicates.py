"""Duplicate-company detection + manual merge (DATA_SPEC §5.3, BACKLOG 4.4).

Detection is deliberately NOT fuzzy (that's a v1 scope cut): companies are
grouped by a normalized "name core" — lowercased, punctuation-stripped, legal
suffixes removed — so "Acme", "Acme Inc." and "Acme, LLC" surface as candidates
an admin can confirm. Merging is always manual.
"""

import re
from collections import defaultdict

from django.db import transaction
from django.db.models import Count

from .models import Activity, Company

# Legal suffixes stripped when computing a name core.
LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "co", "corp", "corporation",
    "gmbh", "plc", "pvt", "private", "pte", "sa", "ag", "bv", "oy", "ab", "srl",
}
FILLABLE_FIELDS = ("domain", "industry", "size", "location")


def name_core(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t and t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def find_duplicate_groups() -> list[list[Company]]:
    """Groups of 2+ companies sharing a name core, widest (most contacts) first."""
    buckets: dict[str, list[Company]] = defaultdict(list)
    for company in Company.objects.annotate(num_contacts=Count("contacts")):
        core = name_core(company.name)
        if core:
            buckets[core].append(company)
    groups = [
        sorted(group, key=lambda c: (-c.num_contacts, c.pk))
        for group in buckets.values()
        if len(group) > 1
    ]
    return sorted(groups, key=lambda g: g[0].name.lower())


@transaction.atomic
def merge_companies(primary: Company, duplicates: list[Company], actor=None) -> int:
    """Move duplicates' contacts onto `primary`, fill its blanks, delete them.

    Returns the number of contacts reassigned. Each moved contact gets a NOTE
    Activity for the audit trail (DATA_SPEC §5.3). Leads follow their contact.
    """
    moved = 0
    for duplicate in duplicates:
        if duplicate.pk == primary.pk:
            continue
        for field in FILLABLE_FIELDS:
            if not getattr(primary, field) and getattr(duplicate, field):
                setattr(primary, field, getattr(duplicate, field))
        for contact in duplicate.contacts.all():
            contact.company = primary
            contact.save(update_fields=["company", "updated_at"])
            Activity.objects.create(
                contact=contact,
                lead=contact.open_lead,
                type=Activity.Type.NOTE,
                actor=actor,
                payload={"text": f"Company merged: {duplicate.name} → {primary.name}"},
            )
            moved += 1
        duplicate.delete()  # before primary.save() so a moved domain can't collide
    primary.save()
    return moved
