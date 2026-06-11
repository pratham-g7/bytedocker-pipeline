import base64
import json
from unittest.mock import MagicMock

import httplib2
import pytest
from django.urls import reverse
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from outreach.models import Mailbox
from outreach.providers import gmail as gmail_module
from outreach.providers.base import ProviderAuthError, TransientProviderError
from outreach.providers.gmail import GmailProvider, _map_http_error

from .factories import MailboxFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


# ---------------------------------------------------------------- settings page


def test_mailboxes_page_shows_configure_hint_when_no_credentials(client, rep, settings):
    settings.GOOGLE_CLIENT_ID = ""
    client.force_login(rep)
    response = client.get(reverse("mailboxes"))
    assert response.status_code == 200
    assert b"GOOGLE_CLIENT_ID" in response.content  # graceful degrade, never a 500


def test_connect_button_enabled_when_configured(client, rep, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    client.force_login(rep)
    response = client.get(reverse("mailboxes"))
    assert reverse("gmail-connect").encode() in response.content


def test_user_sees_only_own_mailboxes(client, rep):
    MailboxFactory(user=rep, email="mine@gmail.com")
    MailboxFactory(email="theirs@gmail.com")
    client.force_login(rep)
    response = client.get(reverse("mailboxes"))
    assert b"mine@gmail.com" in response.content
    assert b"theirs@gmail.com" not in response.content


# ---------------------------------------------------------------- OAuth flow


def test_connect_redirects_to_google_consent(client, rep, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    client.force_login(rep)
    response = client.get(reverse("gmail-connect"))
    assert response.status_code == 302
    assert response.url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert client.session["gmail_oauth_state"]


def test_connect_without_credentials_redirects_home(client, rep, settings):
    settings.GOOGLE_CLIENT_ID = ""
    client.force_login(rep)
    response = client.get(reverse("gmail-connect"))
    assert response.url == reverse("mailboxes")


def _prime_state(client, value="state-123"):
    session = client.session
    session["gmail_oauth_state"] = value
    session.save()


def test_callback_stores_encrypted_token_and_activates(client, rep, monkeypatch):
    token_json = json.dumps({"token": "acc", "refresh_token": "ref"})
    monkeypatch.setattr(gmail_module, "exchange_code", lambda uri, code: token_json)
    monkeypatch.setattr(gmail_module, "profile_email", lambda tok: "Rep@Gmail.com")
    client.force_login(rep)
    _prime_state(client)

    response = client.get(reverse("gmail-callback"), {"code": "c", "state": "state-123"})

    assert response.url == reverse("mailboxes")
    mailbox = Mailbox.objects.get(email="rep@gmail.com")  # lowercased
    assert mailbox.user == rep
    assert mailbox.status == Mailbox.Status.ACTIVE
    assert mailbox.token == token_json
    assert "acc" not in mailbox.oauth_token  # encrypted at rest


def test_callback_reconnect_clears_error_status(client, rep, monkeypatch):
    MailboxFactory(user=rep, email="rep@gmail.com", status=Mailbox.Status.ERROR)
    monkeypatch.setattr(gmail_module, "exchange_code", lambda uri, code: '{"token": "t"}')
    monkeypatch.setattr(gmail_module, "profile_email", lambda tok: "rep@gmail.com")
    client.force_login(rep)
    _prime_state(client)

    client.get(reverse("gmail-callback"), {"code": "c", "state": "state-123"})
    assert Mailbox.objects.get(email="rep@gmail.com").status == Mailbox.Status.ACTIVE


def test_callback_denied_consent_creates_nothing(client, rep):
    client.force_login(rep)
    _prime_state(client)
    response = client.get(reverse("gmail-callback"), {"error": "access_denied"})
    assert response.url == reverse("mailboxes")
    assert not Mailbox.objects.exists()


def test_callback_state_mismatch_creates_nothing(client, rep):
    client.force_login(rep)
    _prime_state(client, "expected")
    client.get(reverse("gmail-callback"), {"code": "c", "state": "forged"})
    assert not Mailbox.objects.exists()


# ---------------------------------------------------------------- edit + test send


def test_mailbox_edit_updates_settings(client, rep):
    mailbox = MailboxFactory(user=rep)
    client.force_login(rep)
    response = client.post(
        reverse("mailbox-edit", args=[mailbox.pk]),
        {
            "daily_cap": 50,
            "send_window_start": "09:00",
            "send_window_end": "17:00",
            "timezone": "Asia/Kolkata",
        },
    )
    assert response.status_code == 204
    mailbox.refresh_from_db()
    assert mailbox.daily_cap == 50
    assert mailbox.timezone == "Asia/Kolkata"


def test_mailbox_edit_rejects_unknown_timezone(client, rep):
    mailbox = MailboxFactory(user=rep)
    client.force_login(rep)
    response = client.post(
        reverse("mailbox-edit", args=[mailbox.pk]),
        {
            "daily_cap": 50,
            "send_window_start": "09:00",
            "send_window_end": "17:00",
            "timezone": "Mars/Olympus",
        },
    )
    assert response.status_code == 200  # re-rendered form, not saved
    mailbox.refresh_from_db()
    assert mailbox.timezone == "UTC"


def test_cannot_edit_another_users_mailbox(client, rep):
    other = MailboxFactory()
    client.force_login(rep)
    assert client.get(reverse("mailbox-edit", args=[other.pk])).status_code == 404


def test_test_send_success_toasts(client, rep, monkeypatch):
    mailbox = MailboxFactory(user=rep)
    mailbox.token = '{"token": "t"}'
    mailbox.save()
    provider = MagicMock()
    provider.send.return_value = ("m1", "t1")
    monkeypatch.setattr("outreach.views.get_provider", lambda mb: provider)
    client.force_login(rep)

    response = client.post(reverse("mailbox-test-send", args=[mailbox.pk]))

    assert response.status_code == 204
    assert "Test email sent" in response["HX-Trigger"]
    assert provider.send.call_args.kwargs["to"] == mailbox.email


def test_test_send_failure_toasts_error(client, rep, monkeypatch):
    mailbox = MailboxFactory(user=rep)
    mailbox.token = '{"token": "t"}'
    mailbox.save()
    provider = MagicMock()
    provider.send.side_effect = TransientProviderError("Gmail returned 503")
    monkeypatch.setattr("outreach.views.get_provider", lambda mb: provider)
    client.force_login(rep)

    response = client.post(reverse("mailbox-test-send", args=[mailbox.pk]))
    assert "error" in response["HX-Trigger"]


def test_test_send_requires_connected_mailbox(client, rep):
    mailbox = MailboxFactory(user=rep)  # no token stored
    client.force_login(rep)
    response = client.post(reverse("mailbox-test-send", args=[mailbox.pk]))
    assert "error" in response["HX-Trigger"]


def test_error_mailbox_banner_shows_on_any_page(client, rep):
    MailboxFactory(user=rep, email="broken@gmail.com", status=Mailbox.Status.ERROR)
    client.force_login(rep)
    response = client.get(reverse("dashboard"))
    assert b"broken@gmail.com" in response.content
    assert b"Reconnect" in response.content


# ---------------------------------------------------------------- GmailProvider


def _http_error(status):
    return HttpError(resp=httplib2.Response({"status": status}), content=b"{}")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_map_to_retryable(status):
    assert isinstance(_map_http_error(_http_error(status)), TransientProviderError)


def test_permanent_4xx_passes_through():
    error = _http_error(400)
    assert _map_http_error(error) is error


def test_send_builds_mime_and_returns_ids(monkeypatch):
    mailbox = MailboxFactory(email="sender@gmail.com")
    service = MagicMock()
    service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "msg-1",
        "threadId": "thr-1",
    }
    monkeypatch.setattr(GmailProvider, "_service", lambda self: service)

    ids = GmailProvider(mailbox).send(
        to="cto@acme.com", subject="Hello Acme", html="<p>Hi</p>", text="Hi"
    )

    assert ids == ("msg-1", "thr-1")
    body = service.users.return_value.messages.return_value.send.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["raw"])
    assert b"To: cto@acme.com" in raw
    assert b"Subject: Hello Acme" in raw
    assert b"text/plain" in raw and b"text/html" in raw
    assert "threadId" not in body


def test_send_with_thread_ref_replies_in_thread(monkeypatch):
    mailbox = MailboxFactory()
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.send.return_value.execute.return_value = {"id": "msg-2", "threadId": "thr-1"}
    # the provider resolves the original's RFC Message-ID from its API id
    messages.get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "Message-Id", "value": "<abc@mail.gmail.com>"}]}
    }
    monkeypatch.setattr(GmailProvider, "_service", lambda self: service)

    GmailProvider(mailbox).send(
        to="cto@acme.com",
        subject="Re: Hello Acme",
        html="<p>Bump</p>",
        text="Bump",
        thread_ref={"message_id": "gmail-api-id-1", "thread_id": "thr-1"},
    )

    assert messages.get.call_args.kwargs["id"] == "gmail-api-id-1"
    body = messages.send.call_args.kwargs["body"]
    assert body["threadId"] == "thr-1"
    raw = base64.urlsafe_b64decode(body["raw"])
    assert b"In-Reply-To: <abc@mail.gmail.com>" in raw
    assert b"References: <abc@mail.gmail.com>" in raw


def test_refresh_failure_flags_mailbox_error():
    mailbox = MailboxFactory(status=Mailbox.Status.ACTIVE)
    creds = MagicMock()
    creds.refresh.side_effect = RefreshError("revoked")

    with pytest.raises(ProviderAuthError):
        GmailProvider(mailbox)._refresh(creds)

    mailbox.refresh_from_db()
    assert mailbox.status == Mailbox.Status.ERROR
