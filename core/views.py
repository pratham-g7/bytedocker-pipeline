import json

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST


def dashboard(request):
    return render(request, "core/dashboard.html")


@require_POST
def toast_demo(request):
    """Phase 0 wiring proof: HX-Trigger toast pattern (UI_SPEC §2.5)."""
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps(
        {"toast": {"level": "success", "msg": "HTMX + Alpine wiring works."}}
    )
    return response
