"""Sandboxed merge-field rendering (DATA_SPEC §3 — outreach).

Templates render against a flat dict of the closed merge-field set below — never
model instances. Validation lives here so EmailTemplate.clean() and the send path
share one definition of "valid"; the render/preview half arrives with task 2.2.
"""

import re

import html2text
from django.core.exceptions import ValidationError

MERGE_FIELDS = frozenset(
    {"first_name", "last_name", "full_name", "company", "title", "sender_name"}
)

# {{field}} or {{field|fallback}} — fallback is plain text, not a Django filter.
_VARIABLE_RE = re.compile(r"\{\{\s*(\w+)\s*(?:\|([^}]*))?\}\}")


def validate_merge_fields(text: str) -> None:
    """Reject anything outside the closed merge-field set (DATA_SPEC §3).

    Raises ValidationError at template save so a broken template can never
    reach a send (ENGINE_SPEC §6 failure-mode table).
    """
    if not text:
        return
    if "{%" in text:
        raise ValidationError("Template tags ({% … %}) are not allowed — only merge fields.")
    unknown = {name for name, _ in _VARIABLE_RE.findall(text) if name not in MERGE_FIELDS}
    if unknown:
        allowed = ", ".join(sorted(MERGE_FIELDS))
        raise ValidationError(
            f"Unknown merge field(s): {', '.join(sorted(unknown))}. Allowed: {allowed}."
        )


def derive_body_text(body_html: str) -> str:
    """Plain-text alternative from HTML (DATA_SPEC §3: auto-derived if blank)."""
    converter = html2text.HTML2Text()
    converter.body_width = 0  # no re-wrapping — keeps {{ merge fields }} intact
    return converter.handle(body_html).strip()
