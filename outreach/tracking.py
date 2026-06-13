"""Open-pixel + click-redirect link building and verification (ENGINE_SPEC §4).

Pure helpers — the public endpoints live in views. Click links are HMAC-signed
over (message uuid, target url) with SECRET_KEY, so the redirect can't be turned
into an open redirect: a tampered url won't verify. URLs are built from BASE_URL.
"""

import base64
import re
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import escape

CLICK_SALT = "outreach.tracking.click"
UNSUBSCRIBE_SALT = "outreach.unsubscribe"

# A constant 1×1 transparent GIF (43 bytes) — served for every open hit.
TRANSPARENT_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# Wrap only absolute http(s) anchors; mailto:, anchors, and the unsubscribe
# footer (appended after wrapping) are left untouched.
_HREF_RE = re.compile(r"""href=(["'])(https?://[^"'>\s]+)\1""", re.IGNORECASE)


def _base() -> str:
    return settings.BASE_URL.rstrip("/")


def _click_sig(message_uuid: str, url: str) -> str:
    return salted_hmac(CLICK_SALT, f"{message_uuid}\n{url}").hexdigest()[:32]


def verify_click(message_uuid, sig: str, url: str) -> bool:
    return constant_time_compare(sig, _click_sig(str(message_uuid), url))


def click_url(message_uuid, target_url: str) -> str:
    sig = _click_sig(str(message_uuid), target_url)
    return f"{_base()}/t/c/{message_uuid}/{sig}/?{urlencode({'u': target_url})}"


def open_pixel_url(message_uuid) -> str:
    return f"{_base()}/t/o/{message_uuid}.gif"


def open_pixel_tag(message_uuid) -> str:
    return (
        f'<img src="{open_pixel_url(message_uuid)}" width="1" height="1" alt=""'
        ' style="display:none">'
    )


def wrap_links(html: str, message_uuid) -> str:
    """Rewrite absolute links through the click redirect (ENGINE_SPEC §4)."""

    def replace(match: re.Match) -> str:
        quote, url = match.group(1), match.group(2)
        return f"href={quote}{click_url(message_uuid, url)}{quote}"

    return _HREF_RE.sub(replace, html)


# ---------------------------------------------------------------- unsubscribe (§4)


def unsubscribe_token(contact) -> str:
    """Signed contact id, no expiry (ENGINE_SPEC §4)."""
    return signing.dumps(contact.pk, salt=UNSUBSCRIBE_SALT)


def parse_unsubscribe_token(token: str):
    """Returns the contact id, or None if the signature is bad."""
    try:
        return signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None


def unsubscribe_url(contact) -> str:
    return f"{_base()}/unsubscribe/{unsubscribe_token(contact)}/"


def unsubscribe_footer(contact) -> str:
    """CAN-SPAM footer: physical address + one-click unsubscribe link."""
    name = escape(getattr(settings, "COMPANY_NAME", "Bytedocker"))
    address = escape(getattr(settings, "COMPANY_ADDRESS", ""))
    return (
        '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #eee;'
        'color:#888;font-size:12px;line-height:1.5">'
        f"{name}{' · ' + address if address else ''}<br>"
        f'<a href="{unsubscribe_url(contact)}" style="color:#888">Unsubscribe</a>'
        "</div>"
    )


def unsubscribe_footer_text(contact) -> str:
    """Plain-text-part equivalent of the footer (no pixel/links to wrap)."""
    name = getattr(settings, "COMPANY_NAME", "Bytedocker")
    address = getattr(settings, "COMPANY_ADDRESS", "")
    lines = ["", "—", f"{name}{' · ' + address if address else ''}"]
    lines.append(f"Unsubscribe: {unsubscribe_url(contact)}")
    return "\n".join(lines)


def list_unsubscribe_headers(contact) -> dict:
    """RFC 8058 one-click unsubscribe headers (Gmail/Yahoo bulk-sender insurance)."""
    return {
        "List-Unsubscribe": f"<{unsubscribe_url(contact)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
