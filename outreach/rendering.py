"""Sandboxed merge-field rendering (DATA_SPEC §3 — outreach).

Templates render with Django's template engine against a flat dict of the
closed merge-field set below — never model instances, attributes, or tags.
validate_merge_fields() is the gate: EmailTemplate.clean() and the preview
view both run it, so only fragments matching the strict ``{{field}}`` /
``{{field|fallback}}`` shape ever reach the engine.
"""

import re

import html2text
from django.core.exceptions import ValidationError
from django.template import Context, Engine

MERGE_FIELDS = frozenset(
    {"first_name", "last_name", "full_name", "company", "title", "sender_name"}
)

# {{field}} or {{field|fallback}} — fallback is plain text, not a Django filter.
_VARIABLE_RE = re.compile(r"\{\{\s*(\w+)\s*(?:\|([^}]*))?\}\}")
_ANY_BRACES_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)

_engine = Engine()  # standalone: no project template dirs, tags, or filters

SAMPLE_CONTEXT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "full_name": "Ada Lovelace",
    "company": "Acme Corp",
    "title": "CTO",
    "sender_name": "Sam Rep",
}


def validate_merge_fields(text: str) -> None:
    """Reject anything outside the closed merge-field set (DATA_SPEC §3).

    Raises ValidationError at template save so a broken template can never
    reach a send (ENGINE_SPEC §6 failure-mode table). Every ``{{ … }}``
    fragment must match the strict shape — attribute access like
    ``{{first_name.upper}}`` is malformed here, keeping the engine sandboxed.
    """
    if not text:
        return
    if "{%" in text:
        raise ValidationError("Template tags ({% … %}) are not allowed — only merge fields.")
    unknown = set()
    for fragment in _ANY_BRACES_RE.findall(text):
        match = _VARIABLE_RE.fullmatch(fragment)
        if not match:
            raise ValidationError(
                f"Malformed merge field: {fragment}. Use {{{{field}}}} or {{{{field|fallback}}}}."
            )
        if match.group(1) not in MERGE_FIELDS:
            unknown.add(match.group(1))
    if unknown:
        allowed = ", ".join(sorted(MERGE_FIELDS))
        raise ValidationError(
            f"Unknown merge field(s): {', '.join(sorted(unknown))}. Allowed: {allowed}."
        )


def render_string(text: str, context: dict, autoescape: bool = False) -> str:
    """Render validated template text against a flat context dict.

    Fallbacks (``{{first_name|there}}``) become ``default`` filters bound to
    injected context keys, so fallback text needs no quote-escaping. Use
    ``autoescape=True`` for HTML bodies; subjects and text parts render raw.
    """
    fallbacks: dict[str, str] = {}

    def _to_engine_syntax(match: re.Match) -> str:
        name, fallback = match.group(1), match.group(2)
        if fallback is None or not fallback.strip():
            return "{{ %s }}" % name
        key = f"fallback{len(fallbacks)}"
        fallbacks[key] = fallback.strip()
        return "{{ %s|default:%s }}" % (name, key)

    prepared = _VARIABLE_RE.sub(_to_engine_syntax, text)
    template = _engine.from_string(prepared)
    return template.render(Context({**context, **fallbacks}, autoescape=autoescape))


def contact_context(contact, mailbox=None) -> dict:
    """The flat dict a real send renders against (closed set, nothing else)."""
    sender = mailbox.user if mailbox else contact.owner
    return {
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "full_name": contact.full_name,
        "company": contact.company.name if contact.company else "",
        "title": contact.title,
        "sender_name": (sender.name or sender.email) if sender else "",
    }


def derive_body_text(body_html: str) -> str:
    """Plain-text alternative from HTML (DATA_SPEC §3: auto-derived if blank)."""
    converter = html2text.HTML2Text()
    converter.body_width = 0  # no re-wrapping — keeps {{ merge fields }} intact
    return converter.handle(body_html).strip()
