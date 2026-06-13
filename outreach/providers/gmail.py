"""GmailProvider + the Gmail OAuth helpers (ENGINE_SPEC §2).

All Google SDK imports live here. Token storage format is google-auth's
authorized-user JSON (Credentials.to_json()), kept encrypted on the Mailbox.
"""

import base64
import json
from email.message import EmailMessage

from django.conf import settings
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import ParsedMessage, ProviderAuthError, TransientProviderError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


# ---------------------------------------------------------------- OAuth flow


def _flow(redirect_uri: str) -> Flow:
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)


def authorization_url(redirect_uri: str) -> tuple[str, str, str]:
    """Returns (url, state, code_verifier). prompt=consent forces a refresh_token grant.

    The PKCE code_verifier is generated here and must survive to exchange_code —
    the callback builds a fresh Flow, so it's carried through the session.
    """
    flow = _flow(redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return url, state, flow.code_verifier


def exchange_code(redirect_uri: str, code: str, code_verifier: str | None = None) -> str:
    """Trade the callback code for the authorized-user JSON Mailbox.token stores."""
    flow = _flow(redirect_uri)
    flow.code_verifier = code_verifier  # PKCE pair from the authorize step
    flow.fetch_token(code=code)
    return flow.credentials.to_json()


def profile_email(token_json: str) -> str:
    """The connected account's address — the Mailbox.email identity."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), scopes=SCOPES)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service.users().getProfile(userId="me").execute()["emailAddress"]


# ---------------------------------------------------------------- provider


class GmailProvider:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def send(self, to, subject, html, text, thread_ref=None, headers=None):
        message = EmailMessage()
        message["To"] = to
        message["From"] = self.mailbox.email
        message["Subject"] = subject
        for name, value in (headers or {}).items():  # e.g. List-Unsubscribe (3.4)
            message[name] = value
        if thread_ref:  # steps 2+ reply in-thread (ENGINE_SPEC §1)
            # Gmail threads on threadId + matching subject + RFC 2822 reply headers.
            # thread_ref carries the API message id; the headers need the original's
            # RFC Message-ID, so resolve it with a metadata lookup.
            rfc_id = self._rfc_message_id(thread_ref["message_id"])
            if rfc_id:
                message["In-Reply-To"] = rfc_id
                message["References"] = rfc_id
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        body = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        if thread_ref:
            body["threadId"] = thread_ref["thread_id"]
        result = self._call(
            lambda service: service.users().messages().send(userId="me", body=body).execute()
        )
        return result["id"], result["threadId"]

    def _rfc_message_id(self, provider_message_id: str) -> str | None:
        result = self._call(
            lambda service: service.users()
            .messages()
            .get(
                userId="me",
                id=provider_message_id,
                format="metadata",
                metadataHeaders=["Message-Id"],
            )
            .execute()
        )
        for header in result.get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == "message-id":
                return header.get("value")
        return None  # still sent with threadId — Gmail usually threads regardless

    def fetch_new_messages(self, cursor):
        """New INBOX messages since `cursor` (a Gmail historyId), + the new cursor.

        Cursor-safe (ENGINE_SPEC §3): on the first poll there is no baseline
        historyId, so we record the current one and return nothing — replies
        only start matching from the next tick. The cursor advances only after
        the caller commits, so a crash re-delivers (handlers are idempotent).
        """
        if not cursor:
            profile = self._call(
                lambda service: service.users().getProfile(userId="me").execute()
            )
            return [], str(profile["historyId"])

        history = self._call(
            lambda service: service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=cursor,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
        new_cursor = str(history.get("historyId", cursor))
        seen, parsed = set(), []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id in seen:
                    continue
                seen.add(msg_id)
                if "INBOX" not in added["message"].get("labelIds", []):
                    continue  # sent/draft echoes carry other labels
                parsed.append(self._parse_message(msg_id))
        return parsed, new_cursor

    def _parse_message(self, msg_id: str) -> ParsedMessage:
        msg = self._call(
            lambda service: service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata")
            .execute()
        )
        headers = {
            h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])
        }
        return ParsedMessage(
            provider_message_id=msg_id,
            thread_id=msg.get("threadId", ""),
            from_addr=headers.get("from", ""),
            subject=headers.get("subject", ""),
            snippet=msg.get("snippet", ""),
            headers=headers,
        )

    def refresh_token(self):
        self._refresh(self._credentials())

    # -- internals -------------------------------------------------------

    def _credentials(self) -> Credentials:
        return Credentials.from_authorized_user_info(json.loads(self.mailbox.token), SCOPES)

    def _service(self):
        creds = self._credentials()
        if not creds.valid:
            creds = self._refresh(creds)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _refresh(self, creds: Credentials) -> Credentials:
        """Refresh in place; revoked grant flags the mailbox `error` (ENGINE_SPEC §2)."""
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            self.mailbox.status = self.mailbox.Status.ERROR
            self.mailbox.save(update_fields=["status", "updated_at"])
            raise ProviderAuthError(f"Gmail token refresh failed for {self.mailbox}") from exc
        self.mailbox.token = creds.to_json()
        self.mailbox.save(update_fields=["oauth_token", "updated_at"])
        return creds

    def _call(self, fn):
        try:
            return fn(self._service())
        except HttpError as exc:
            if exc.resp.status == 401:  # stale access token — refresh once, retry
                self._refresh(self._credentials())
                try:
                    return fn(self._service())
                except HttpError as retry_exc:
                    raise _map_http_error(retry_exc) from retry_exc
            raise _map_http_error(exc) from exc


def _map_http_error(exc: HttpError) -> Exception:
    if exc.resp.status == 429 or exc.resp.status >= 500:
        return TransientProviderError(f"Gmail returned {exc.resp.status}")
    return exc  # permanent 4xx — bubbles to send_step's failure handling (2.6)
