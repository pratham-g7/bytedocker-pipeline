"""Public, login-exempt endpoints (ENGINE_SPEC §4): open pixel + click redirect.

These are hit by recipients' mail clients, so they bypass LoginRequiredMiddleware
via @login_not_required. Open/click timestamps are first-event-wins.
"""

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.http import require_GET

from pipeline.models import Activity

from .models import Message
from .tracking import TRANSPARENT_GIF, verify_click


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
