import json
import time
from unittest.mock import MagicMock

import pytest
from django.urls import reverse

from outreach.models import Mailbox
from outreach.providers import get_provider
from outreach.providers import gmail as gmail_module
from outreach.providers import graph as graph_module
from outreach.providers.base import ProviderAuthError, TransientProviderError
from outreach.providers.gmail import GmailProvider
from outreach.providers.graph import GraphProvider

from .factories import MailboxFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(db):
    return UserFactory(email="rep@x.com", role="rep")


def _outlook_mailbox(**kwargs):
    mailbox = MailboxFactory(provider=Mailbox.Provider.OUTLOOK, **kwargs)
    mailbox.token = json.dumps(
        {"access_token": "acc", "refresh_token": "ref", "expires_at": time.time() + 3600}
    )
    mailbox.save()
    return mailbox


def _response(status_code, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.content = json.dumps(payload).encode() if payload is not None else b""
    if status_code < 400:
        response.raise_for_status.return_value = None
    return response


# ---------------------------------------------------------------- OAuth flow


def test_connect_redirects_to_microsoft_consent(client, rep, settings):
    settings.MS_CLIENT_ID = "ms-client"
    client.force_login(rep)
    response = client.get(reverse("outlook-connect"))
    assert response.status_code == 302
    assert response.url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/")
    assert "offline_access" in response.url
    assert client.session["ms_oauth_state"]


def test_connect_without_credentials_redirects_home(client, rep, settings):
    settings.MS_CLIENT_ID = ""
    client.force_login(rep)
    assert client.get(reverse("outlook-connect")).url == reverse("mailboxes")


def test_callback_creates_outlook_mailbox(client, rep, monkeypatch):
    token_json = json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 1})
    monkeypatch.setattr(graph_module, "exchange_code", lambda uri, code: token_json)
    monkeypatch.setattr(graph_module, "profile_email", lambda tok: "Rep@Outlook.com")
    client.force_login(rep)
    session = client.session
    session["ms_oauth_state"] = "st"
    session.save()

    response = client.get(reverse("outlook-callback"), {"code": "c", "state": "st"})

    assert response.url == reverse("mailboxes")
    mailbox = Mailbox.objects.get(email="rep@outlook.com")
    assert mailbox.provider == Mailbox.Provider.OUTLOOK
    assert mailbox.status == Mailbox.Status.ACTIVE
    assert mailbox.token == token_json
    assert "acc" not in mailbox.oauth_token


# ---------------------------------------------------------------- provider


def test_get_provider_routes_by_provider():
    assert isinstance(get_provider(MailboxFactory(provider="gmail")), GmailProvider)
    assert isinstance(get_provider(_outlook_mailbox()), GraphProvider)


def test_send_drafts_then_sends_and_returns_ids(monkeypatch):
    mailbox = _outlook_mailbox()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/me/messages"):
            return _response(201, {"id": "draft-1", "conversationId": "conv-1"})
        return _response(202)

    monkeypatch.setattr(graph_module.requests, "request", fake_request)

    ids = GraphProvider(mailbox).send(
        to="cto@acme.com", subject="Hello", html="<p>Hi</p>", text="Hi"
    )

    assert ids == ("draft-1", "conv-1")
    draft_call = calls[0]
    assert draft_call[0] == "POST"
    assert draft_call[2]["toRecipients"] == [{"emailAddress": {"address": "cto@acme.com"}}]
    assert calls[1][1].endswith("/me/messages/draft-1/send")


def test_send_with_thread_ref_uses_create_reply(monkeypatch):
    mailbox = _outlook_mailbox()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/createReply"):
            return _response(201, {"id": "reply-1", "conversationId": "conv-1"})
        return _response(200, {})

    monkeypatch.setattr(graph_module.requests, "request", fake_request)

    GraphProvider(mailbox).send(
        to="cto@acme.com",
        subject="Re: Hello",
        html="<p>Bump</p>",
        text="Bump",
        thread_ref={"message_id": "orig-1", "thread_id": "conv-1"},
    )

    assert calls[0] == ("POST", f"{graph_module.GRAPH}/me/messages/orig-1/createReply")
    assert calls[1][0] == "PATCH"
    assert calls[2] == ("POST", f"{graph_module.GRAPH}/me/messages/reply-1/send")


def test_429_maps_to_transient_error(monkeypatch):
    mailbox = _outlook_mailbox()
    monkeypatch.setattr(graph_module.requests, "request", lambda *a, **k: _response(429))
    with pytest.raises(TransientProviderError):
        GraphProvider(mailbox).send(to="x@y.com", subject="s", html="<p>h</p>", text="t")


def test_401_refreshes_and_retries_once(monkeypatch):
    mailbox = _outlook_mailbox()
    request_calls = []

    def fake_request(method, url, **kwargs):
        request_calls.append(url)
        if len(request_calls) == 1:
            return _response(401)
        if url.endswith("/me/messages"):
            return _response(201, {"id": "d1", "conversationId": "c1"})
        return _response(202)

    refreshed = _response(
        200, {"access_token": "new-acc", "refresh_token": "new-ref", "expires_in": 3600}
    )
    monkeypatch.setattr(graph_module.requests, "request", fake_request)
    monkeypatch.setattr(graph_module.requests, "post", lambda *a, **k: refreshed)

    ids = GraphProvider(mailbox).send(to="x@y.com", subject="s", html="<p>h</p>", text="t")

    assert ids == ("d1", "c1")
    mailbox.refresh_from_db()
    assert json.loads(mailbox.token)["access_token"] == "new-acc"


def test_refresh_failure_flags_mailbox_error(monkeypatch):
    mailbox = _outlook_mailbox()
    monkeypatch.setattr(
        graph_module.requests, "post", lambda *a, **k: _response(400, {"error": "invalid_grant"})
    )
    with pytest.raises(ProviderAuthError):
        GraphProvider(mailbox)._refresh()
    mailbox.refresh_from_db()
    assert mailbox.status == Mailbox.Status.ERROR


def test_expired_token_refreshes_before_call(monkeypatch):
    mailbox = MailboxFactory(provider=Mailbox.Provider.OUTLOOK)
    mailbox.token = json.dumps(
        {"access_token": "old", "refresh_token": "ref", "expires_at": time.time() - 10}
    )
    mailbox.save()
    refreshed = _response(
        200, {"access_token": "fresh", "refresh_token": "ref2", "expires_in": 3600}
    )
    monkeypatch.setattr(graph_module.requests, "post", lambda *a, **k: refreshed)

    assert GraphProvider(mailbox)._access_token() == "fresh"


def test_gmail_module_unaffected_by_graph_import():
    # the SDK boundary holds: both providers coexist behind get_provider
    assert hasattr(gmail_module, "GmailProvider")
    assert hasattr(graph_module, "GraphProvider")
