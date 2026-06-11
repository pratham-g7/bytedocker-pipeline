import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from outreach.models import EmailTemplate
from outreach.rendering import (
    SAMPLE_CONTEXT,
    contact_context,
    render_string,
    validate_merge_fields,
)

from .factories import ContactFactory, EmailTemplateFactory, MailboxFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


# ---------------------------------------------------------------- rendering


def test_render_substitutes_merge_fields():
    out = render_string("Hi {{first_name}} at {{company}}", SAMPLE_CONTEXT)
    assert out == "Hi Ada at Acme Corp"


def test_fallback_applies_when_field_blank():
    context = {**SAMPLE_CONTEXT, "first_name": ""}
    assert render_string("Hi {{first_name|there}}", context) == "Hi there"


def test_fallback_ignored_when_field_present():
    assert render_string("Hi {{first_name|there}}", SAMPLE_CONTEXT) == "Hi Ada"


def test_html_render_escapes_merge_values():
    context = {**SAMPLE_CONTEXT, "company": "Smith & Sons <Ltd>"}
    out = render_string("<p>{{company}}</p>", context, autoescape=True)
    assert "Smith &amp; Sons &lt;Ltd&gt;" in out


def test_attribute_access_rejected_by_validation():
    with pytest.raises(ValidationError, match="Malformed"):
        validate_merge_fields("Hi {{first_name.upper}}")


def test_contact_context_uses_mailbox_sender():
    contact = ContactFactory(first_name="Grace", title="VP Eng")
    mailbox = MailboxFactory(user=UserFactory(name="Sam Rep"))
    context = contact_context(contact, mailbox)
    assert context["first_name"] == "Grace"
    assert context["company"] == contact.company.name
    assert context["sender_name"] == "Sam Rep"


def test_contact_context_falls_back_to_owner_and_handles_no_company():
    owner = UserFactory(name="", email="owner@x.com")
    contact = ContactFactory(company=None, owner=owner)
    context = contact_context(contact)
    assert context["company"] == ""
    assert context["sender_name"] == "owner@x.com"


# ---------------------------------------------------------------- views


def test_templates_list_renders(client, rep):
    EmailTemplateFactory(name="Intro email")
    client.force_login(rep)
    response = client.get(reverse("templates"))
    assert response.status_code == 200
    assert b"Intro email" in response.content


def test_editor_page_renders(client, rep):
    template = EmailTemplateFactory()
    client.force_login(rep)
    response = client.get(reverse("template-edit", args=[template.pk]))
    assert response.status_code == 200
    assert b"template-preview" in response.content


def test_create_template_redirects_to_editor(client, rep):
    client.force_login(rep)
    response = client.post(
        reverse("template-create"),
        {"name": "Cold intro", "subject": "Hi {{first_name|there}}", "body_html": "<p>Hello</p>"},
    )
    assert response.status_code == 204
    template = EmailTemplate.objects.get(name="Cold intro")
    assert response["HX-Redirect"] == reverse("template-edit", args=[template.pk])
    assert template.body_text  # derived from HTML at save


def test_save_with_unknown_field_shows_form_error_not_500(client, rep):
    client.force_login(rep)
    response = client.post(
        reverse("template-create"),
        {"name": "Bad", "subject": "Hey {{nickname}}", "body_html": "<p>x</p>"},
    )
    assert response.status_code == 200  # re-rendered form partial
    assert b"nickname" in response.content
    assert not EmailTemplate.objects.exists()


def test_preview_renders_with_sample_contact(client, rep):
    client.force_login(rep)
    response = client.post(
        reverse("template-preview"),
        {"subject": "For {{company}}", "body_html": "<p>Hi {{first_name|there}}</p>"},
    )
    assert response.status_code == 200
    assert b"Acme Corp" in response.content
    assert b"Hi Ada" in response.content


def test_preview_with_unknown_field_shows_error(client, rep):
    client.force_login(rep)
    response = client.post(
        reverse("template-preview"),
        {"subject": "x", "body_html": "<p>{{nickname}}</p>"},
    )
    assert response.status_code == 200
    assert b"Unknown merge field" in response.content
