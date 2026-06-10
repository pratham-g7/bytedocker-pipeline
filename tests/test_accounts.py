import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

User = get_user_model()
pytestmark = pytest.mark.django_db


def test_create_user_normalizes_email_to_lowercase():
    user = User.objects.create_user("Jane.Doe@Example.COM", "pass1234x")
    assert user.email == "jane.doe@example.com"


def test_default_role_is_rep():
    user = User.objects.create_user("rep@example.com", "pass1234x")
    assert user.role == User.Role.REP
    assert not user.is_staff


def test_create_superuser_is_admin_role():
    admin = User.objects.create_superuser("boss@example.com", "pass1234x")
    assert admin.role == User.Role.ADMIN
    assert admin.is_staff and admin.is_superuser


def test_duplicate_email_rejected_case_insensitively():
    User.objects.create_user("dup@example.com", "pass1234x")
    with pytest.raises(IntegrityError):  # both normalize to the same address
        User.objects.create_user("DUP@example.com", "pass1234x")
