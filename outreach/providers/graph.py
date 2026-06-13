"""GraphProvider + the Outlook OAuth helpers (ENGINE_SPEC §2).

Plain Microsoft identity-platform / Graph REST via requests — no MSAL.
Token storage format: {"access_token", "refresh_token", "expires_at"} JSON,
kept encrypted on the Mailbox.

Sends go draft → send (POST /me/messages, then /send) instead of /sendMail,
because /sendMail returns 202 with no body — the draft response is the only
way to capture the message id + conversationId the Message row needs.
"""

import json
import time
from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import ParsedMessage, ProviderAuthError, TransientProviderError

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Mail.Send Mail.Read"
TIMEOUT = 30


def _authority() -> str:
    return f"https://login.microsoftonline.com/{settings.MS_TENANT or 'common'}"


# ---------------------------------------------------------------- OAuth flow


def authorization_url(redirect_uri: str, state: str) -> str:
    params = urlencode(
        {
            "client_id": settings.MS_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
            "prompt": "select_account",
        }
    )
    return f"{_authority()}/oauth2/v2.0/authorize?{params}"


def exchange_code(redirect_uri: str, code: str, code_verifier: str | None = None) -> str:
    """Trade the callback code for the token JSON Mailbox.token stores.

    code_verifier is accepted for a uniform callback signature; Graph's
    confidential-client flow uses the client secret, not PKCE.
    """
    response = requests.post(
        f"{_authority()}/oauth2/v2.0/token",
        data={
            "client_id": settings.MS_CLIENT_ID,
            "client_secret": settings.MS_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return _token_json(response.json())


def profile_email(token_json: str) -> str:
    """The connected account's address — the Mailbox.email identity."""
    access_token = json.loads(token_json)["access_token"]
    response = requests.get(
        f"{GRAPH}/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("mail") or data["userPrincipalName"]


def _token_json(payload: dict, fallback_refresh: str = "") -> str:
    return json.dumps(
        {
            "access_token": payload["access_token"],
            # refresh responses may omit the refresh token — keep the old one
            "refresh_token": payload.get("refresh_token") or fallback_refresh,
            "expires_at": time.time() + payload.get("expires_in", 3600),
        }
    )


# ---------------------------------------------------------------- provider


class GraphProvider:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def send(self, to, subject, html, text, thread_ref=None, headers=None):
        # Graph only accepts custom (X-prefixed) internetMessageHeaders; standard
        # headers like List-Unsubscribe can't be set this way, so they're dropped
        # for Outlook (the in-body unsubscribe link remains the primary path).
        custom_headers = [
            {"name": k, "value": v}
            for k, v in (headers or {}).items()
            if k.lower().startswith("x-")
        ]
        body = {"subject": subject, "body": {"contentType": "HTML", "content": html}}
        if custom_headers:
            body["internetMessageHeaders"] = custom_headers
        if thread_ref:  # steps 2+ reply in-thread (ENGINE_SPEC §1; completed in 2.8)
            draft = self._request("POST", f"/me/messages/{thread_ref['message_id']}/createReply")
            self._request("PATCH", f"/me/messages/{draft['id']}", json=body)
        else:
            body["toRecipients"] = [{"emailAddress": {"address": to}}]
            draft = self._request("POST", "/me/messages", json=body)
        self._request("POST", f"/me/messages/{draft['id']}/send")
        return draft["id"], draft.get("conversationId", "")

    def fetch_new_messages(self, cursor):
        """New Inbox messages via delta query (ENGINE_SPEC §3), + the new deltaLink.

        `cursor` is a full Graph deltaLink URL (or empty on the first poll).
        We page through nextLinks, collecting messages, until Graph hands back
        the deltaLink to use next time. Cursor advances only after the caller
        commits, so a crash re-delivers (handlers are idempotent).
        """
        select = "id,conversationId,subject,from,bodyPreview,internetMessageHeaders"
        url = cursor or (
            f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select={select}"
        )
        parsed: list[ParsedMessage] = []
        new_cursor = cursor
        while url:
            page = self._request_url("GET", url)
            for msg in page.get("value", []):
                if "@removed" in msg:
                    continue  # deletions — not relevant to reply matching
                parsed.append(self._parse_message(msg))
            if page.get("@odata.nextLink"):
                url = page["@odata.nextLink"]
            else:
                new_cursor = page.get("@odata.deltaLink", cursor)
                url = None
        return parsed, new_cursor

    @staticmethod
    def _parse_message(msg: dict) -> ParsedMessage:
        headers = {
            h["name"].lower(): h["value"] for h in msg.get("internetMessageHeaders", [])
        }
        return ParsedMessage(
            provider_message_id=msg.get("id", ""),
            thread_id=msg.get("conversationId", ""),
            from_addr=(msg.get("from") or {}).get("emailAddress", {}).get("address", ""),
            subject=msg.get("subject", ""),
            snippet=msg.get("bodyPreview", ""),
            headers=headers,
        )

    def refresh_token(self):
        self._refresh()

    # -- internals -------------------------------------------------------

    def _request(self, method, path, json=None):
        return self._request_url(method, GRAPH + path, json=json)

    def _request_url(self, method, url, json=None, _retried=False):
        response = requests.request(
            method,
            url,
            json=json,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=TIMEOUT,
        )
        if response.status_code == 401 and not _retried:  # stale token — refresh, retry once
            self._refresh()
            return self._request_url(method, url, json=json, _retried=True)
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientProviderError(f"Graph returned {response.status_code}")
        response.raise_for_status()
        return response.json() if response.content else None

    def _access_token(self) -> str:
        data = json.loads(self.mailbox.token)
        if data.get("expires_at", 0) < time.time() + 60:  # proactive refresh near expiry
            data = self._refresh()
        return data["access_token"]

    def _refresh(self) -> dict:
        """Refresh in place; revoked grant flags the mailbox `error` (ENGINE_SPEC §2)."""
        stored = json.loads(self.mailbox.token)
        response = requests.post(
            f"{_authority()}/oauth2/v2.0/token",
            data={
                "client_id": settings.MS_CLIENT_ID,
                "client_secret": settings.MS_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": stored.get("refresh_token", ""),
                "scope": SCOPES,
            },
            timeout=TIMEOUT,
        )
        payload = response.json()
        if response.status_code != 200 or "access_token" not in payload:
            self.mailbox.status = self.mailbox.Status.ERROR
            self.mailbox.save(update_fields=["status", "updated_at"])
            raise ProviderAuthError(f"Graph token refresh failed for {self.mailbox}")
        token_json = _token_json(payload, fallback_refresh=stored.get("refresh_token", ""))
        self.mailbox.token = token_json
        self.mailbox.save(update_fields=["oauth_token", "updated_at"])
        return json.loads(token_json)
