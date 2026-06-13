"""Public, login-exempt endpoints (ENGINE_SPEC §4): open pixel + click redirect.

These are hit by recipients' mail clients, so they bypass LoginRequiredMiddleware
via @login_not_required. Open/click timestamps are first-event-wins.
"""

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from pipeline.models import Activity, Contact

from .models import Enrollment, Message
from .tracking import TRANSPARENT_GIF, parse_unsubscribe_token, verify_click


def _gif_response() -> HttpResponse:
    response = HttpResponse(TRANSPARENT_GIF, content_type="image/gif")
    # No caching, so re-opens still register; the GIF body itself is a constant.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@login_not_required
@require_GET
def track_open(request, uuid):
    message = Message.objects.select_related("enrollment__contact").filter(uuid=uuid).first()
    if message and message.opened_at is None:  # first-event-wins (DATA_SPEC §3)
        message.opened_at = timezone.now()
        message.save(update_fields=["opened_at", "updated_at"])
        contact = message.enrollment.contact
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.EMAIL_OPENED,
            payload={"message_id": str(message.uuid)},
        )
    return _gif_response()  # unknown uuid still returns the GIF — no probing oracle


@login_not_required
@require_GET
def track_click(request, uuid, sig):
    url = request.GET.get("u", "")
    # Bad signature → 404; the scheme check blocks javascript:/data: open redirects.
    if not url.lower().startswith(("http://", "https://")) or not verify_click(uuid, sig, url):
        raise Http404
    message = Message.objects.select_related("enrollment__contact").filter(uuid=uuid).first()
    if message and message.clicked_at is None:  # first-event-wins
        message.clicked_at = timezone.now()
        message.save(update_fields=["clicked_at", "updated_at"])
        contact = message.enrollment.contact
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.EMAIL_CLICKED,
            payload={"message_id": str(message.uuid), "url": url[:500]},
        )
    return HttpResponseRedirect(url)


@login_not_required
@csrf_exempt  # RFC 8058 one-click POST comes from the mail client, no CSRF token
def unsubscribe(request, token):
    """GET shows a confirm page; POST (footer button or one-click) suppresses (§4)."""
    contact_id = parse_unsubscribe_token(token)
    contact = Contact.objects.filter(pk=contact_id).first() if contact_id is not None else None
    if contact is None:
        raise Http404
    if request.method == "POST":
        _suppress(contact)
    context = {"contact": contact, "done": contact.unsubscribed_at is not None}
    return render(request, "outreach/unsubscribe.html", context)


def _suppress(contact):
    """Set unsubscribed_at + terminal-ize live enrollments (idempotent)."""
    already = contact.unsubscribed_at is not None
    if not already:
        contact.unsubscribed_at = timezone.now()
        contact.save(update_fields=["unsubscribed_at", "updated_at"])
    live = list(
        contact.enrollments.filter(status__in=[Enrollment.Status.ACTIVE, Enrollment.Status.PAUSED])
    )
    for enrollment in live:
        enrollment.mark_unsubscribed(payload={"via": "email-link"})
    if not already and not live:  # record the suppression even with no live enrollment
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.UNSUBSCRIBED,
            payload={"via": "email-link"},
        )
