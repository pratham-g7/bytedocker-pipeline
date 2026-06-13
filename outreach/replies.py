"""Inbound classification for reply polling (ENGINE_SPEC §3).

Pure functions over a ParsedMessage — no DB, no network — so the heuristics
are exhaustively unit-testable. The task layer (tasks.py) decides what to do
with the verdict.
"""

from .providers.base import ParsedMessage

# Subject prefixes that mark an automated away-message (case-insensitive).
AUTO_SUBJECT_MARKERS = (
    "out of office",
    "out-of-office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "auto reply",
    "away from",
)

# Sender localparts that mark a delivery-status / bounce notification.
BOUNCE_SENDERS = ("mailer-daemon", "postmaster")


def is_auto_reply(msg: ParsedMessage) -> bool:
    """OOO / vacation autoresponders — these do NOT pause the sequence (§3)."""
    headers = msg.headers
    # RFC 3834: Auto-Submitted is auto-generated/auto-replied (anything but "no").
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted.startswith("auto"):
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    if headers.get("precedence", "").strip().lower() in ("bulk", "auto_reply", "junk"):
        return True
    subject = (msg.subject or "").lower()
    return any(marker in subject for marker in AUTO_SUBJECT_MARKERS)


def is_bounce(msg: ParsedMessage) -> bool:
    """Delivery-status notifications — terminal-ize the enrollment (§3)."""
    sender = (msg.from_addr or "").lower()
    if any(localpart in sender for localpart in BOUNCE_SENDERS):
        return True
    content_type = msg.headers.get("content-type", "").lower()
    return "multipart/report" in content_type and "delivery-status" in content_type
