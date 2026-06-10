import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db):
    return User.objects.create_user("rep@example.com", "pass1234x")


def test_anonymous_is_redirected_to_login(client):
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302
    assert reverse("login") in response.url


def test_login_page_is_public(client):
    assert client.get(reverse("login")).status_code == 200


def test_dashboard_renders_for_authenticated_user(client, user):
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert b"Phase 0 wiring check" in response.content


def test_toast_demo_sets_hx_trigger(client, user):
    client.force_login(user)
    response = client.post(reverse("toast-demo"))
    assert response.status_code == 204
    assert "toast" in response.headers["HX-Trigger"]
