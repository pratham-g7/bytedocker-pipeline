from django import template

register = template.Library()

ACTIVITY_ICONS = {
    "email_sent": "✉️",
    "email_opened": "👀",
    "email_clicked": "🔗",
    "email_replied": "📨",
    "email_bounced": "⚠️",
    "note": "📝",
    "call": "📞",
    "stage_change": "🔁",
    "task_done": "✅",
    "enrolled": "➕",
    "unsubscribed": "🚫",
    "import": "📥",
}


@register.filter
def activity_icon(activity_type):
    return ACTIVITY_ICONS.get(activity_type, "•")


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
