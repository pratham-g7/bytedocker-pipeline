import pytest
from django.urls import reverse

from outreach.models import Message
from outreach.tracking import click_url, open_pixel_tag, verify_click, wrap_links
from pipeline.models import Activity

from .factories import MessageFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- signing / wrapping


def test_click_signature_round_trips():
    uuid = "11111111-1111-1111-1111-111111111111"
    url = "https://acme.com/pricing"
    signed = click_url(uuid, url)
    sig = signed.split("/t/c/")[1].split("/")[1]
    assert verify_click(uuid, sig, url)


def test_tampered_url_fails_verification():
    uuid = "11111111-1111-1111-1111-111111111111"
    signed = click_url(uuid, "https://acme.com/pricing")
    sig = signed.split("/t/c/")[1].split("/")[1]
    assert not verify_click(uuid, sig, "https://evil.com")


def test_wrap_links_rewrites_absolute_anchors_only():
    html = '<a href="https://acme.com/x">x</a> <a href="mailto:a@b.com">mail</a>'
    uuid = "22222222-2222-2222-2222-222222222222"
    wrapped = wrap_links(html, uuid)
    assert f"/t/c/{uuid}/" in wrapped
    assert "u=https%3A%2F%2Facme.com%2Fx" in wrapped
    assert 'href="mailto:a@b.com"' in wrapped  # untouched


def test_open_pixel_tag_targets_the_message():
    uuid = "33333333-3333-3333-3333-333333333333"
    assert f"/t/o/{uuid}.gif" in open_pixel_tag(uuid)


# ---------------------------------------------------------------- open endpoint


def test_open_sets_timestamp_and_logs_once(client):
    message = MessageFactory(status=Message.Status.SENT)
    url = reverse("track-open", args=[message.uuid])

    first = client.get(url)
    assert first.status_code == 200
    assert first["Content-Type"] == "image/gif"
    message.refresh_from_db()
    opened = message.opened_at
    assert opened is not None
    assert (
        Activity.objects.filter(
            contact=message.enrollment.contact, type=Activity.Type.EMAIL_OPENED
        ).count()
        == 1
    )

    client.get(url)  # re-open (MPP proxies fire repeatedly)
    message.refresh_from_db()
    assert message.opened_at == opened  # first-event-wins
    assert Activity.objects.filter(type=Activity.Type.EMAIL_OPENED).count() == 1


def test_open_is_public_no_login():
    from django.test import Client

    message = MessageFactory()
    response = Client().get(reverse("track-open", args=[message.uuid]))  # anonymous
    assert response.status_code == 200


def test_unknown_uuid_still_returns_gif(client):
    response = client.get(reverse("track-open", args=["44444444-4444-4444-4444-444444444444"]))
    assert response.status_code == 200  # no probing oracle
    assert response["Content-Type"] == "image/gif"


# ---------------------------------------------------------------- click endpoint


def _click_url_for(message, target):
    signed = click_url(message.uuid, target)
    return signed[signed.index("/t/c/") :]  # strip BASE_URL to a test path


def test_valid_click_redirects_and_records(client):
    message = MessageFactory(status=Message.Status.SENT)
    target = "https://acme.com/pricing"

    response = client.get(_click_url_for(message, target))

    assert response.status_code == 302
    assert response["Location"] == target
    message.refresh_from_db()
    assert message.clicked_at is not None
    activity = Activity.objects.get(
        contact=message.enrollment.contact, type=Activity.Type.EMAIL_CLICKED
    )
    assert activity.payload["url"] == target


def test_bad_signature_404s(client):
    message = MessageFactory()
    bad = f"/t/c/{message.uuid}/deadbeefdeadbeefdeadbeefdeadbeef/?u=https://acme.com"
    assert client.get(bad).status_code == 404
    message.refresh_from_db()
    assert message.clicked_at is None


def test_click_first_event_wins(client):
    message = MessageFactory(status=Message.Status.SENT)
    path = _click_url_for(message, "https://acme.com/x")

    client.get(path)
    message.refresh_from_db()
    first = message.clicked_at
    client.get(path)
    message.refresh_from_db()
    assert message.clicked_at == first
    assert Activity.objects.filter(type=Activity.Type.EMAIL_CLICKED).count() == 1
