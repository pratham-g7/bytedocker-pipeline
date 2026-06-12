import json

from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.permissions import scope_to_user
from outreach.models import Enrollment, Mailbox
from pipeline.models import Activity, Lead, Task


def dashboard(request):
    user = request.user
    enrollments = scope_to_user(Enrollment.objects, user, field="contact__owner")
    mailboxes = Mailbox.objects.all() if user.role != "rep" else user.mailboxes.all()
    sends = mailboxes.aggregate(today=Sum("sends_today"), cap=Sum("daily_cap"))
    context = {
        "open_leads": scope_to_user(Lead.objects.filter(status=Lead.Status.OPEN), user).count(),
        "active_enrollments": enrollments.filter(status=Enrollment.Status.ACTIVE).count(),
        "replied": enrollments.filter(status=Enrollment.Status.REPLIED).count(),
        "sends_today": sends["today"] or 0,
        "daily_cap": sends["cap"] or 0,
        "my_tasks": Task.objects.filter(owner=user, done_at__isnull=True)
        .select_related("lead__contact")
        .order_by("due_at")[:6],
        "activities": scope_to_user(
            Activity.objects.select_related("contact", "actor"), user, field="contact__owner"
        )[:8],
        "now": timezone.now(),
    }
    return render(request, "core/dashboard.html", context)


@require_POST
def toast_demo(request):
    """Phase 0 wiring proof: HX-Trigger toast pattern (UI_SPEC §2.5)."""
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps(
        {"toast": {"level": "success", "msg": "HTMX + Alpine wiring works."}}
    )
    return response
