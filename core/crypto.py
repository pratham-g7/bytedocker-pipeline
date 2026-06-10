"""Symmetric encryption for secrets at rest (Mailbox OAuth tokens).

Uses Fernet with a single key from FIELD_ENCRYPTION_KEY. Key rotation, if ever
needed, means decrypt-all + re-encrypt — acceptable at this scale.
"""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

GENERATE_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(f"FIELD_ENCRYPTION_KEY is not set. {GENERATE_HINT}")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
