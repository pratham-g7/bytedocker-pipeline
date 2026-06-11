"""MailProvider protocol + the exception contract (ENGINE_SPEC §2)."""

from typing import Protocol


class TransientProviderError(Exception):
    """Retryable send failure (429/5xx). Celery autoretry hooks this (ENGINE_SPEC §1)."""


class ProviderAuthError(Exception):
    """Token refresh failed (revoked). The mailbox is flagged `error`; not retryable."""


class MailProvider(Protocol):
    def send(
        self, to: str, subject: str, html: str, text: str, thread_ref: dict | None = None
    ) -> tuple[str, str]:
        """Deliver one message; returns (provider_message_id, thread_id)."""
        ...

    def fetch_new_messages(self, cursor: str) -> tuple[list, str]:
        """Reply polling (Phase 3): returns (messages, new_cursor)."""
        ...

    def refresh_token(self) -> None: ...
