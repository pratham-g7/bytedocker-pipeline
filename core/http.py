import json

from django.http import HttpResponse


def hx_events(events: dict, status: int = 204) -> HttpResponse:
    """Empty response carrying HX-Trigger events (UI_SPEC §2)."""
    response = HttpResponse(status=status)
    response["HX-Trigger"] = json.dumps(events)
    return response


def hx_toast(msg: str, level: str = "success", extra_events: dict | None = None) -> HttpResponse:
    events = {"toast": {"level": level, "msg": msg}}
    if extra_events:
        events.update(extra_events)
    return hx_events(events)
