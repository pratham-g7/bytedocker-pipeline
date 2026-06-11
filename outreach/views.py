import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.http import hx_toast
from core.permissions import scope_to_user
from pipeline.models import Activity, Contact

from .forms import EmailTemplateForm, EnrollForm, MailboxSettingsForm, SequenceForm, StepForm
from .models import (
    EmailTemplate,
    Enrollment,
    InvalidTransition,
    Mailbox,
    Sequence,
    SequenceStep,
    scope_sequences,
)
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


# ---------------------------------------------------------------- sequences


def sequences_list(request):
    qs = (
        scope_sequences(Sequence.objects.select_related("owner"), request.user)
        .annotate(
            step_count=Count("steps", distinct=True),
            active_count=Count(
                "enrollments", filter=Q(enrollments__status="active"), distinct=True
            ),
            replied_count=Count(
                "enrollments", filter=Q(enrollments__status="replied"), distinct=True
            ),
            finished_count=Count(
                "enrollments", filter=Q(enrollments__status="finished"), distinct=True
            ),
        )
        .order_by("name")
    )
    context = {"sequences": qs}
    template = "outreach/_sequences_table.html" if request.htmx else "outreach/sequences_list.html"
    return render(request, template, context)


def sequence_create(request):
    if request.method == "POST":
        form = SequenceForm(request.POST)
        if form.is_valid():
            sequence = form.save(commit=False)
            sequence.owner = request.user
            sequence.save()
            response = hx_toast("Sequence created — add steps.", extra_events={"close-modal": True})
            response["HX-Redirect"] = reverse("sequence-detail", args=[sequence.pk])
            return response
    else:
        form = SequenceForm()
    return render(
        request,
        "pipeline/_modal_form.html",
        {"form": form, "modal_title": "New sequence", "action": reverse("sequence-create")},
    )


def _get_sequence(request, pk):
    return get_object_or_404(scope_sequences(Sequence.objects, request.user), pk=pk)


def _steps_with_days(sequence):
    """Step cards read 'Day N · template' (UI_SPEC §3) — N is cumulative wait."""
    steps = list(sequence.steps.select_related("template"))
    day = 0
    for step in steps:
        day += step.wait_days
        step.day = day
    return steps


def sequence_detail(request, pk):
    sequence = _get_sequence(request, pk)
    context = {
        "sequence": sequence,
        "steps": _steps_with_days(sequence),
        "locked": sequence.is_locked,
        "step_form": StepForm(),
        "status_filter": request.GET.get("status", ""),
        "statuses": Enrollment.Status.choices,
        "enrollments": _sequence_enrollments(sequence, request.GET.get("status", "")),
    }
    return render(request, "outreach/sequence_detail.html", context)


def sequence_steps(request, pk):
    sequence = _get_sequence(request, pk)
    context = {
        "sequence": sequence,
        "steps": _steps_with_days(sequence),
        "locked": sequence.is_locked,
        "step_form": StepForm(),
    }
    return render(request, "outreach/_steps.html", context)


def _sequence_enrollments(sequence, status):
    qs = sequence.enrollments.select_related("contact", "mailbox").order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return qs[:100]


def sequence_enrollments(request, pk):
    sequence = _get_sequence(request, pk)
    status = request.GET.get("status", "")
    context = {
        "sequence": sequence,
        "status_filter": status,
        "statuses": Enrollment.Status.choices,
        "enrollments": _sequence_enrollments(sequence, status),
    }
    return render(request, "outreach/_enrollments.html", context)


@require_POST
def sequence_toggle(request, pk):
    sequence = _get_sequence(request, pk)
    sequence.is_active = not sequence.is_active
    sequence.save(update_fields=["is_active", "updated_at"])
    state = "active" if sequence.is_active else "inactive"
    return hx_toast(f"Sequence is now {state}.", extra_events={"refresh-steps": True})


@require_POST
def sequence_clone(request, pk):
    source = _get_sequence(request, pk)
    clone = Sequence.objects.create(
        name=f"{source.name} (copy)", owner=request.user, is_active=source.is_active
    )
    SequenceStep.objects.bulk_create(
        SequenceStep(
            sequence=clone, order=step.order, wait_days=step.wait_days, template=step.template
        )
        for step in source.steps.all()
    )
    response = hx_toast("Sequence cloned — this copy is editable.")
    response["HX-Redirect"] = reverse("sequence-detail", args=[clone.pk])
    return response


@require_POST
def step_add(request, pk):
    sequence = _get_sequence(request, pk)
    if sequence.is_locked:
        return hx_toast("Sequence has enrollments — clone it to edit.", level="error")
    form = StepForm(request.POST)
    if not form.is_valid():
        return hx_toast("Pick a template and a wait.", level="error")
    step = form.save(commit=False)
    step.sequence = sequence
    step.order = (sequence.steps.aggregate(m=Max("order"))["m"] or 0) + 1
    step.save()
    return hx_toast(f"Step {step.order} added.", extra_events={"refresh-steps": True})


@require_POST
def step_delete(request, pk):
    step = get_object_or_404(
        SequenceStep.objects.filter(
            sequence__in=scope_sequences(Sequence.objects, request.user)
        ).select_related("sequence"),
        pk=pk,
    )
    if step.sequence.is_locked:
        return hx_toast("Sequence has enrollments — clone it to edit.", level="error")
    step.delete()
    # Keep orders contiguous 1..n — the sender loop walks current_step + 1 (2.6).
    for index, remaining in enumerate(step.sequence.steps.order_by("order"), start=1):
        if remaining.order != index:
            remaining.order = index
            remaining.save(update_fields=["order", "updated_at"])
    return hx_toast("Step removed.", extra_events={"refresh-steps": True})


def step_preview(request, pk):
    step = get_object_or_404(
        SequenceStep.objects.filter(
            sequence__in=scope_sequences(Sequence.objects, request.user)
        ).select_related("template"),
        pk=pk,
    )
    template = step.template
    context = {
        "step": step,
        "subject": render_string(template.subject, SAMPLE_CONTEXT),
        "body_html": render_string(template.body_html, SAMPLE_CONTEXT, autoescape=True),
        "sample": SAMPLE_CONTEXT,
    }
    return render(request, "outreach/_step_preview.html", context)


# ---------------------------------------------------------------- enrollment


def enroll_modal(request):
    contact_ids = request.GET.getlist("cid")
    contacts = scope_to_user(Contact.objects, request.user).filter(pk__in=contact_ids)
    if not contacts:
        return hx_toast("Select at least one contact first.", level="error")
    form = EnrollForm(request.user)
    context = {"form": form, "contacts": contacts}
    return render(request, "outreach/_enroll_modal.html", context)


@require_POST
def enroll(request):
    form = EnrollForm(request.user, request.POST)
    contacts = scope_to_user(Contact.objects, request.user).filter(
        pk__in=request.POST.getlist("cid")
    )
    if not form.is_valid() or not contacts:
        return hx_toast("Pick a sequence and a sending mailbox.", level="error")
    sequence = form.cleaned_data["sequence"]
    mailbox = form.cleaned_data["mailbox"]
    created = skipped = 0
    for contact in contacts:
        if contact.unsubscribed_at or contact.bounced_at:
            skipped += 1
            continue
        try:
            with transaction.atomic():  # ride the partial-unique constraint (DATA_SPEC §3)
                enrollment = Enrollment.objects.create(
                    contact=contact,
                    sequence=sequence,
                    mailbox=mailbox,
                    enrolled_by=request.user,
                    next_send_at=timezone.now(),  # step 1 due now, window-gated (2.6)
                )
        except IntegrityError:
            skipped += 1
            continue
        Activity.objects.create(
            contact=contact,
            lead=contact.open_lead,
            type=Activity.Type.ENROLLED,
            actor=request.user,
            payload={"sequence": sequence.name, "enrollment_id": enrollment.pk},
        )
        created += 1
    msg = f"Enrolled {created} contact{'s' if created != 1 else ''} in {sequence.name}."
    if skipped:
        msg += f" Skipped {skipped} (already enrolled, unsubscribed, or bounced)."
    return hx_toast(
        msg,
        level="success" if created else "error",
        extra_events={
            "close-modal": True,
            "refresh-enrollments": True,
            "refresh-timeline": True,
        },
    )


@require_POST
def enrollment_action(request, pk):
    enrollment = get_object_or_404(
        scope_to_user(Enrollment.objects, request.user, field="contact__owner"), pk=pk
    )
    action = request.POST.get("action")
    try:
        if action == "pause":
            enrollment.pause()
        elif action == "resume":
            enrollment.resume()
        elif action == "stop":
            enrollment.mark_finished()
        else:
            return hx_toast("Unknown action.", level="error")
    except InvalidTransition:
        return hx_toast(
            f"Cannot {action} a {enrollment.get_status_display().lower()} enrollment.",
            level="error",
        )
    return hx_toast(
        f"Enrollment {enrollment.get_status_display().lower()}.",
        extra_events={"refresh-enrollments": True},
    )


def contact_enrollments(request, pk):
    """Right-rail card on contact detail (UI_SPEC §3)."""
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    enrollments = contact.enrollments.select_related("sequence", "mailbox").order_by("-created_at")
    return render(
        request,
        "outreach/_contact_enrollments.html",
        {"contact": contact, "enrollments": enrollments},
    )
