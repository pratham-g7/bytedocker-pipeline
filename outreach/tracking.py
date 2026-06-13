"""Open-pixel + click-redirect link building and verification (ENGINE_SPEC §4).

Pure helpers — the public endpoints live in views. Click links are HMAC-signed
over (message uuid, target url) with SECRET_KEY, so the redirect can't be turned
into an open redirect: a tampered url won't verify. URLs are built from BASE_URL.
"""

import base64
import re
from urllib.parse import urlencode

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

CLICK_SALT = "outreach.tracking.click"

# A constant 1×1 transparent GIF (43 bytes) — served for every open hit.
TRANSPARENT_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

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
