def mailbox_alerts(request):
    """Surfaces the owner's broken mailboxes as a banner on every page (ENGINE_SPEC §2)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"error_mailboxes": request.user.mailboxes.filter(status="error")}
