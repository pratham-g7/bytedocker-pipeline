import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _encryption_key(settings):
    """Tests never depend on the developer's .env for the encryption key."""
    settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()
