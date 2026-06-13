from unittest.mock import MagicMock

import pytest
from django.urls import reverse

from accounts.models import Team
from ingestion import enrichment
from ingestion.enrichment import (
    ApolloProvider,
    EnrichmentUnavailable,
    enrich_contact,
    resolve_key,
    resolve_provider,
)
from pipeline.models import Activity

from .factories import ContactFactory, UserFactory

pytestmark = pytest.mark.django_db

APOLLO_PERSON = {
    "person": {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "title": "CTO",
        "email": "ada@acme.com",
        "linkedin_url": "https://linkedin.com/in/ada",
        "organization": {"name": "Acme", "primary_domain": "acme.com"},
    }
}


@pytest.fixture
def apollo(monkeypatch):
    """Stub the Apollo HTTP call; returns the response object it should yield."""

    def configure(payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        monkeypatch.setattr(enrichment.requests, "post", lambda *a, **k: response)

    return configure


# ---------------------------------------------------------------- key resolution


def test_team_key_takes_precedence(settings):
    settings.APOLLO_API_KEY = "global"
    team = Team.objects.create(name="Alpha", enrichment_api_key="team-key")
    user = UserFactory(team=team)
    assert resolve_key(user) == "team-key"


def test_falls_back_to_global_key(settings):
    settings.APOLLO_API_KEY = "global"
    assert resolve_key(UserFactory()) == "global"


def test_no_key_means_no_provider(settings):
    settings.APOLLO_API_KEY = ""
    assert resolve_provider(UserFactory()) is None


# ---------------------------------------------------------------- provider parse


def test_apollo_parses_person(apollo):
    apollo(APOLLO_PERSON)
    result = ApolloProvider("k").match(
        first_name="Ada", last_name="Lovelace", company_name="Acme", company_domain="", email=""
    )
    assert result["title"] == "CTO"
    assert result["email"] == "ada@acme.com"
    assert result["company_domain"] == "acme.com"
    assert result["raw"]["title"] == "CTO"


def test_apollo_no_match_returns_none(apollo):
    apollo({"person": None})
    assert (
        ApolloProvider("k").match(
            first_name="X", last_name="Y", company_name="", company_domain="", email=""
        )
        is None
    )


# ---------------------------------------------------------------- enrich_contact


def test_enrich_fills_only_blanks_and_stores_raw(apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo(APOLLO_PERSON)
    contact = ContactFactory(title="", first_name="Ada", last_name="Lovelace", linkedin_url="")

    matched = enrich_contact(contact)

    assert matched
    contact.refresh_from_db()
    assert contact.title == "CTO"  # was blank → filled
    assert contact.linkedin_url == "https://linkedin.com/in/ada"
    assert contact.custom_fields["enrichment"]["title"] == "CTO"  # raw stored
    assert Activity.objects.filter(contact=contact, type=Activity.Type.NOTE).exists()


def test_enrich_does_not_overwrite_existing(apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo(APOLLO_PERSON)
    contact = ContactFactory(title="Head of Eng")
    enrich_contact(contact)
    contact.refresh_from_db()
    assert contact.title == "Head of Eng"  # not overwritten


def test_enrich_without_key_raises(settings):
    settings.APOLLO_API_KEY = ""
    with pytest.raises(EnrichmentUnavailable):
        enrich_contact(ContactFactory())


def test_enrich_no_match_returns_false(apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo({"person": None})
    assert enrich_contact(ContactFactory()) is False


# ---------------------------------------------------------------- views


def test_enrich_button_enriches(client, apollo, settings):
    settings.APOLLO_API_KEY = "global"
    apollo(APOLLO_PERSON)
    rep = UserFactory(email="rep@x.com", role="rep")
    contact = ContactFactory(owner=rep, title="")
    client.force_login(rep)

    response = client.post(reverse("contact-enrich", args=[contact.pk]))

    assert response.status_code == 204
    assert response["HX-Refresh"] == "true"
    contact.refresh_from_db()
    assert contact.title == "CTO"


def test_enrich_without_key_is_friendly(client, settings):
    settings.APOLLO_API_KEY = ""
    rep = UserFactory(email="rep@x.com", role="rep")
    contact = ContactFactory(owner=rep)
    client.force_login(rep)
    response = client.post(reverse("contact-enrich", args=[contact.pk]))
    assert "error" in response["HX-Trigger"]


def test_enrich_scoped_to_owner(client):
    rep = UserFactory(email="rep@x.com", role="rep")
    other = ContactFactory()
    client.force_login(rep)
    assert client.post(reverse("contact-enrich", args=[other.pk])).status_code == 404


def test_admin_sets_team_key(client):
    team = Team.objects.create(name="Alpha")
    admin = UserFactory(email="admin@x.com", role="admin", team=team)
    client.force_login(admin)
    response = client.post(reverse("enrichment-settings"), {"enrichment_api_key": "secret-key"})
    assert response.status_code == 204
    team.refresh_from_db()
    assert team.enrichment_api_key == "secret-key"


def test_key_settings_requires_team(client):
    admin = UserFactory(email="admin@x.com", role="admin")  # no team
    client.force_login(admin)
    response = client.post(reverse("enrichment-settings"), {"enrichment_api_key": "x"})
    assert "error" in response["HX-Trigger"]
