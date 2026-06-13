import pytest
from django.urls import reverse

from pipeline.duplicates import find_duplicate_groups, merge_companies, name_core
from pipeline.models import Activity, Company

from .factories import CompanyFactory, ContactFactory, LeadFactory, UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- detection


def test_name_core_strips_legal_suffixes():
    assert name_core("Acme Inc.") == "acme"
    assert name_core("Acme, LLC") == "acme"
    assert name_core("Acme Corporation") == "acme"
    assert name_core("Beta Co") == "beta"
    assert name_core("Northwind Health") == "northwind health"


def test_finds_groups_of_two_or_more():
    CompanyFactory(name="Acme", domain=None)
    CompanyFactory(name="Acme Inc", domain="acme.com")
    CompanyFactory(name="Unique Co", domain="unique.com")  # singleton, not a group

    groups = find_duplicate_groups()

    assert len(groups) == 1
    assert {c.name for c in groups[0]} == {"Acme", "Acme Inc"}


def test_group_ordered_by_contact_count():
    small = CompanyFactory(name="Acme", domain=None)
    big = CompanyFactory(name="Acme Inc", domain="acme.com")
    ContactFactory(company=big)
    ContactFactory(company=big)
    ContactFactory(company=small)

    group = find_duplicate_groups()[0]
    assert group[0] == big  # widest first → suggested primary


# ---------------------------------------------------------------- merge


def test_merge_reassigns_contacts_fills_blanks_and_audits():
    primary = CompanyFactory(name="Acme", domain=None, industry="")
    dup = CompanyFactory(name="Acme Inc", domain="acme.com", industry="SaaS")
    c1 = ContactFactory(company=dup)
    c2 = ContactFactory(company=dup)
    LeadFactory(contact=c1)

    moved = merge_companies(primary, [dup], actor=None)

    assert moved == 2
    primary.refresh_from_db()
    assert primary.domain == "acme.com"  # blank filled from the duplicate
    assert primary.industry == "SaaS"
    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.company == primary and c2.company == primary
    assert not Company.objects.filter(pk=dup.pk).exists()  # duplicate removed
    note = Activity.objects.filter(contact=c1, type=Activity.Type.NOTE).first()
    assert "merged" in note.payload["text"].lower()


def test_merge_keeps_primary_existing_fields():
    primary = CompanyFactory(name="Acme", domain="primary.com")
    dup = CompanyFactory(name="Acme Inc", domain="dup.com")
    merge_companies(primary, [dup])
    primary.refresh_from_db()
    assert primary.domain == "primary.com"  # not overwritten


# ---------------------------------------------------------------- views


def test_duplicates_page_admin_only(client):
    client.force_login(UserFactory(email="rep@x.com", role="rep"))
    assert client.get(reverse("duplicates")).status_code == 403


def test_merge_view_merges(client):
    admin = UserFactory(email="admin@x.com", role="admin")
    primary = CompanyFactory(name="Acme", domain=None)
    dup = CompanyFactory(name="Acme Inc", domain="acme.com")
    ContactFactory(company=dup)
    client.force_login(admin)

    response = client.post(reverse("company-merge"), {"primary": primary.pk, "dup": [dup.pk]})

    assert response.status_code == 204
    assert not Company.objects.filter(pk=dup.pk).exists()


def test_merge_view_admin_only(client):
    rep = UserFactory(email="rep@x.com", role="rep")
    primary = CompanyFactory(domain=None)
    client.force_login(rep)
    assert client.post(reverse("company-merge"), {"primary": primary.pk}).status_code == 403


def test_merge_view_no_duplicates_is_friendly(client):
    admin = UserFactory(email="admin@x.com", role="admin")
    primary = CompanyFactory(domain=None)
    client.force_login(admin)
    response = client.post(reverse("company-merge"), {"primary": primary.pk, "dup": [primary.pk]})
    assert "error" in response["HX-Trigger"]  # primary excluded from its own merge
