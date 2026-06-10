from django.conf import settings
from django.core.checks import Warning, register

from .crypto import GENERATE_HINT


@register()
def encryption_key_check(app_configs, **kwargs):
    if not settings.FIELD_ENCRYPTION_KEY:
        return [
            Warning(
                "FIELD_ENCRYPTION_KEY is not set; mailbox OAuth tokens cannot be stored.",
                hint=GENERATE_HINT,
                id="core.W001",
            )
        ]
    return []
