import pytest
from django.urls import reverse
from django.utils import timezone

from outreach.models import Enrollment
from outreach.tasks import dispatch_due_sends
from outreach.tracking import (
    list_unsubscribe_headers,
    unsubscribe_footer,
    unsubscribe_token,
    unsubscribe_url,
)
from pipeline.models import Activity

from .factories import ContactFactory, EnrollmentFactory, SequenceStepFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- helpers


def test_footer_and_headers_contain_signed_link():
    contact = ContactFactory()
    token = unsubscribe_token(contact)
    assert token in unsubscribe_url(contact)
    assert token in unsubscribe_footer(contact)
    headers = list_unsubscribe_headers(contact)
    assert token in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


# ---------------------------------------------------------------- endpoint


def _unsub_path(contact):
    return reverse("unsubscribe", args=[unsubscribe_token(contact)])


def test_get_shows_confirm_then_post_suppresses(client):
    enrollment = EnrollmentFactory()
    contact = enrollment.contact
    path = _unsub_path(contact)

    page = client.get(path)
    assert page.status_code == 200
    assert b"Unsubscribe from emails?" in page.content

    done = client.post(path)
    assert done.status_code == 200
    assert b"unsubscribed" in done.content.lower()

    contact.refresh_from_db()
    assert contact.unsubscribed_at is not None
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.UNSUBSCRIBED
    assert Activity.objects.filter(contact=contact, type=Activity.Type.UNSUBSCRIBED).exists()


def test_one_click_post_works_without_csrf():
    from django.test import Client

    contact = ContactFactory()
    # enforce_csrf_checks=True mimics the mail client's cross-site one-click POST
    client = Client(enforce_csrf_checks=True)
    response = client.post(_unsub_path(contact))
    assert response.status_code == 200
    contact.refresh_from_db()
    assert contact.unsubscribed_at is not None


def test_unsubscribe_is_idempotent(client):
    contact = ContactFactory()
    path = _unsub_path(contact)
    client.post(path)
    contact.refresh_from_db()
    first = contact.unsubscribed_at
    client.post(path)
    contact.refresh_from_db()
    assert contact.unsubscribed_at == first
    assert Activity.objects.filter(contact=contact, type=Activity.Type.UNSUBSCRIBED).count() == 1


def test_contact_with_no_enrollment_still_records(client):
    contact = ContactFactory()
    client.post(_unsub_path(contact))
    contact.refresh_from_db()
    assert contact.unsubscribed_at is not None
    assert Activity.objects.filter(contact=contact, type=Activity.Type.UNSUBSCRIBED).count() == 1


def test_bad_token_404s(client):
    assert client.get(reverse("unsubscribe", args=["not-a-real-token"])).status_code == 404


def test_endpoint_is_public(client):
    from django.test import Client

    contact = ContactFactory()
    assert Client().get(_unsub_path(contact)).status_code == 200  # anonymous


# ---------------------------------------------------------------- suppression at dispatch


def test_unsubscribed_contact_never_sends_again(client, monkeypatch):
    from datetime import datetime
    from datetime import timezone as dt_timezone

    sent = []
    provider = type("P", (), {"send": lambda self, **kw: sent.append(kw) or ("m", "t")})()
    monkeypatch.setattr("outreach.tasks.get_provider", lambda mb: provider)

    enrollment = EnrollmentFactory(next_send_at=timezone.now())
    SequenceStepFactory(sequence=enrollment.sequence, order=1, wait_days=0)

    client.post(_unsub_path(enrollment.contact))  # unsubscribe between ticks

    # window-open mailbox so only suppression can stop the send
    enrollment.refresh_from_db()
    dispatch_due_sends(now=datetime(2026, 6, 10, 12, 0, tzinfo=dt_timezone.utc))

    assert sent == []  # never delivered
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.UNSUBSCRIBED
