"""MailProvider protocol + the exception contract (ENGINE_SPEC §2)."""

from dataclasses import dataclass, field
from typing import Protocol


class TransientProviderError(Exception):
    """Retryable send failure (429/5xx). Celery autoretry hooks this (ENGINE_SPEC §1)."""


class ProviderAuthError(Exception):
    """Token refresh failed (revoked). The mailbox is flagged `error`; not retryable."""


@dataclass
class ParsedMessage:
    """A normalized inbound message from any provider (reply polling, ENGINE_SPEC §3).

    `headers` keys are lowercased so classification (auto-reply/bounce) is
    provider-agnostic. `thread_id` matches our sent Message.thread_id.
    """

    provider_message_id: str
    thread_id: str
    from_addr: str
    subject: str
    snippet: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class MailProvider(Protocol):
    def send(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
        thread_ref: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[str, str]:
        """Deliver one message; returns (provider_message_id, thread_id)."""
        ...

    def fetch_new_messages(self, cursor: str) -> tuple[list[ParsedMessage], str]:
        """Reply polling (Phase 3): returns (messages, new_cursor)."""
        ...

    def refresh_token(self) -> None: ...
