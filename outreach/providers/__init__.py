"""Provider abstraction boundary (ENGINE_SPEC §2).

Hard rule: nothing outside this package imports the Google/Microsoft SDKs.
Views and tasks go through get_provider() / the module-level OAuth helpers.
"""

from .base import MailProvider, ProviderAuthError, TransientProviderError


def get_provider(mailbox) -> MailProvider:
    if mailbox.provider == mailbox.Provider.GMAIL:
        from .gmail import GmailProvider

        return GmailProvider(mailbox)
    if mailbox.provider == mailbox.Provider.OUTLOOK:
        from .graph import GraphProvider

        return GraphProvider(mailbox)
    raise ValueError(f"No provider for {mailbox.provider!r}")


__all__ = ["MailProvider", "ProviderAuthError", "TransientProviderError", "get_provider"]
