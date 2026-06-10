import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _encryption_key(settings):
    """Tests never depend on the developer's .env for the encryption key."""
    settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    """Manifest storage needs a collectstatic run; tests shouldn't depend on one."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
