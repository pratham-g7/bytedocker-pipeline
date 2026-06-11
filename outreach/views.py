import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.http import hx_toast

from .forms import EmailTemplateForm, MailboxSettingsForm
from .models import EmailTemplate, Mailbox
from .providers import get_provider
from .providers import gmail as gmail_provider
from .providers import graph as graph_provider
from .rendering import SAMPLE_CONTEXT, render_string, validate_merge_fields

# ---------------------------------------------------------------- templates


def templates_list(request):
    qs = EmailTemplate.objects.annotate(step_count=Count("steps")).order_by("name")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(name__icontains=q)
    context = {"templates": qs}
    template = "outreach/_templates_table.html" if request.htmx else "outreach/templates_list.html"
    return render(request, template, context)


def template_create(request):
    return _template_editor(request, None)


def template_edit(request, pk):
    return _template_editor(request, get_object_or_404(EmailTemplate, pk=pk))


def _template_editor(request, template_obj):
    action = (
        reverse("template-edit", args=[template_obj.pk])
        if template_obj
        else reverse("template-create")
    )
    if request.method == "POST":
        form = EmailTemplateForm(request.POST, instance=template_obj)
        if form.is_valid():
            obj = form.save()
            if template_obj:
                return hx_toast("Template saved.")
            response = hx_toast("Template created.")
            response["HX-Redirect"] = reverse("template-edit", args=[obj.pk])
            return response
        return render(request, "outreach/_editor_form.html", {"form": form, "action": action})
    form = EmailTemplateForm(instance=template_obj)
    return render(
        request,
        "outreach/template_editor.html",
        {"form": form, "action": action, "template_obj": template_obj},
    )


@require_POST
def template_preview(request):
    """Re-render the preview pane against the sample contact (UI_SPEC §3)."""
    subject = request.POST.get("subject", "")
    body_html = request.POST.get("body_html", "")
    try:
        validate_merge_fields(subject)
        validate_merge_fields(body_html)
    except ValidationError as exc:
        return render(request, "outreach/_preview.html", {"error": "; ".join(exc.messages)})
    context = {
        "subject": render_string(subject, SAMPLE_CONTEXT),
        "body_html": render_string(body_html, SAMPLE_CONTEXT, autoescape=True),
        "sample": SAMPLE_CONTEXT,
    }
    return render(request, "outreach/_preview.html", context)


# ---------------------------------------------------------------- mailboxes


def _redirect_uri(url_name):
    return settings.BASE_URL.rstrip("/") + reverse(url_name)


def mailboxes_settings(request):
    """Personal page: each user manages only their own mailboxes (UI_SPEC §5)."""
    context = {
        "mailboxes": request.user.mailboxes.order_by("email"),
        "gmail_configured": bool(settings.GOOGLE_CLIENT_ID),
        "outlook_configured": bool(settings.MS_CLIENT_ID),
    }
    template = "outreach/_mailboxes.html" if request.htmx else "outreach/mailboxes.html"
    return render(request, template, context)


def gmail_connect(request):
    if not settings.GOOGLE_CLIENT_ID:
        return redirect("mailboxes")  # button is disabled; belt-and-braces
    url, state = gmail_provider.authorization_url(_redirect_uri("gmail-callback"))
    request.session["gmail_oauth_state"] = state
    return redirect(url)


def gmail_callback(request):
    return _finish_oauth_callback(
        request,
        gmail_provider,
        Mailbox.Provider.GMAIL,
        state_key="gmail_oauth_state",
        redirect_uri=_redirect_uri("gmail-callback"),
    )


def outlook_connect(request):
    if not settings.MS_CLIENT_ID:
        return redirect("mailboxes")
    state = secrets.token_urlsafe(24)
    request.session["ms_oauth_state"] = state
    return redirect(graph_provider.authorization_url(_redirect_uri("outlook-callback"), state))


def outlook_callback(request):
    return _finish_oauth_callback(
        request,
        graph_provider,
        Mailbox.Provider.OUTLOOK,
        state_key="ms_oauth_state",
        redirect_uri=_redirect_uri("outlook-callback"),
    )


def _finish_oauth_callback(request, provider_module, provider, state_key, redirect_uri):
    state = request.session.pop(state_key, None)
    if (
        "code" not in request.GET
        or "error" in request.GET
        or not state
        or request.GET.get("state") != state
    ):
        return redirect("mailboxes")  # denied consent / stale state — no mailbox change
    token_json = provider_module.exchange_code(redirect_uri, request.GET["code"])
    email = provider_module.profile_email(token_json).lower()
    mailbox, _ = Mailbox.objects.update_or_create(
        email=email,
        defaults={
            "user": request.user,
            "provider": provider,
            "status": Mailbox.Status.ACTIVE,  # reconnect clears a previous error
        },
    )
    mailbox.token = token_json  # encrypted at rest via core.crypto
    mailbox.save(update_fields=["oauth_token", "updated_at"])
    return redirect("mailboxes")


def mailbox_edit(request, pk):
    mailbox = get_object_or_404(request.user.mailboxes, pk=pk)
    if request.method == "POST":
        form = MailboxSettingsForm(request.POST, instance=mailbox)
        if form.is_valid():
            form.save()
            return hx_toast(
                "Mailbox settings saved.",
                extra_events={"close-modal": True, "refresh-mailboxes": True},
            )
    else:
        form = MailboxSettingsForm(instance=mailbox)
    return render(
        request,
        "pipeline/_modal_form.html",
        {
            "form": form,
            "modal_title": f"Mailbox settings — {mailbox.email}",
            "action": reverse("mailbox-edit", args=[mailbox.pk]),
        },
    )


@require_POST
def mailbox_test_send(request, pk):
    """Manual deliverability check (BACKLOG 2.3 AC): sends a real email to the mailbox itself."""
    mailbox = get_object_or_404(request.user.mailboxes, pk=pk)
    if mailbox.status != Mailbox.Status.ACTIVE or not mailbox.oauth_token:
        return hx_toast("Mailbox is not connected — reconnect first.", level="error")
    provider = get_provider(mailbox)
    try:
        provider.send(
            to=mailbox.email,
            subject="Bytedocker test send",
            html="<p>Your mailbox is connected and can send. 🐳</p>",
            text="Your mailbox is connected and can send.",
        )
    except Exception as exc:
        return hx_toast(f"Test send failed: {exc}", level="error")
    return hx_toast(f"Test email sent to {mailbox.email} — check the inbox.")
