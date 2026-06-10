import pytest
from django.core.exceptions import ImproperlyConfigured

from core import crypto


def test_round_trip():
    token = crypto.encrypt("oauth-secret-payload")
    assert token != "oauth-secret-payload"
    assert crypto.decrypt(token) == "oauth-secret-payload"


def test_missing_key_raises_clear_error(settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    with pytest.raises(ImproperlyConfigured, match="FIELD_ENCRYPTION_KEY"):
        crypto.encrypt("anything")
