from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def icon(name, **attrs):
    """Inline an SVG sprite symbol (static/img/icons.svg) — presentation only.

    Usage: {% icon "board" class="h-4 w-4" %}
    """
    css = attrs.get("class", "h-4 w-4")
    return format_html(
        '<svg class="{}" aria-hidden="true" focusable="false"><use href="{}#{}"/></svg>',
        css,
        static("img/icons.svg"),
        name,
    )


# Line-icon sprite names + tone classes for the activity feed (presentation only).
ACTIVITY_ICON_NAMES = {
    "email_sent": "mailbox",
    "email_opened": "eye",
    "email_clicked": "integrations",
    "email_replied": "reply",
    "email_bounced": "alert",
    "note": "pencil",
    "call": "contacts",
    "stage_change": "arrow-right",
    "task_done": "check",
    "enrolled": "sequences",
    "unsubscribed": "x",
    "import": "imports",
}
# Values are CSS classes (act-emerald, act-red, …) defined in static/src/app.css.
ACTIVITY_TONES = {
    "email_replied": "act-emerald",
    "task_done": "act-emerald",
    "email_bounced": "act-red",
    "unsubscribed": "act-red",
    "email_clicked": "act-accent",
    "enrolled": "act-accent",
    "email_opened": "act-sky",
}


@register.filter
def activity_icon_name(activity_type):
    return ACTIVITY_ICON_NAMES.get(activity_type, "check")


@register.filter
def activity_tone(activity_type):
    return ACTIVITY_TONES.get(activity_type, "act-neutral")


@register.filter
def activity_text(activity):
    p = activity.payload
    match activity.type:
        case "stage_change":
            return f"Moved {p.get('from', '?')} → {p.get('to', '?')}"
        case "note":
            return p.get("text", "")
        case "task_done":
            return f"Completed task: {p.get('title', '')}"
        case "import":
            return f"Imported ({p.get('source', 'csv')})"
        case "enrolled":
            return f"Enrolled in {p.get('sequence', 'a sequence')}"
        case _:
            return activity.get_type_display()


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        params[key] = value
    return params.urlencode()


@register.simple_tag(takes_context=True)
def nav_active(context, *prefixes):
    """Returns 'nav-active' when the current path matches a prefix ('/' = exact)."""
    path = context["request"].path
    for prefix in prefixes:
        if prefix == "/" and path == "/":
            return "nav-active"
        if prefix != "/" and path.startswith(prefix):
            return "nav-active"
    return ""
