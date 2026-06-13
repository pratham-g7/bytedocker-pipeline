import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.http import hx_toast
from core.permissions import role_required, scope_to_user

from .duplicates import find_duplicate_groups, merge_companies
from .forms import CompanyForm, ContactForm, TaskForm
from .gdpr import contact_export_data
from .models import Activity, Company, Contact, Lead, Stage, Task, create_open_lead

# ---------------------------------------------------------------- contacts


def contacts_list(request):
    qs = scope_to_user(Contact.objects.select_related("company", "owner"), request.user)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
            | Q(company__name__icontains=q)
        )
    if owner := request.GET.get("owner"):
        qs = qs.filter(owner_id=owner)
    if source := request.GET.get("source"):
        qs = qs.filter(source=source)
    page = Paginator(qs.order_by("-created_at"), 25).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "target": "#contacts-table",
        "sources": Contact.objects.exclude(source="")
        .values_list("source", flat=True)
        .distinct()
        .order_by("source"),
        "owners": _owner_choices(request.user),
    }
    template = "pipeline/_contacts_table.html" if request.htmx else "pipeline/contacts_list.html"
    return render(request, template, context)


def _owner_choices(user):
    if user.role == "rep":
        return None
    return get_user_model().objects.filter(is_active=True).order_by("email")


def contact_create(request):
    return _contact_form(request, None)


def contact_edit(request, pk):
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    return _contact_form(request, contact)


def _contact_form(request, contact):
    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact)
        _restrict_owner_field(form, request.user)
        if form.is_valid():
            obj = form.save(commit=False)
            if not obj.owner:
                obj.owner = request.user
            obj.save()
            return hx_toast(
                f"Contact {'updated' if contact else 'created'}.",
                extra_events={"close-modal": True, "refresh-contacts": True},
            )
    else:
        form = ContactForm(instance=contact)
        _restrict_owner_field(form, request.user)
    action = reverse("contact-edit", args=[contact.pk]) if contact else reverse("contact-create")
    title = "Edit contact" if contact else "New contact"
    return render(
        request, "pipeline/_modal_form.html", {"form": form, "modal_title": title, "action": action}
    )


def _restrict_owner_field(form, user):
    if user.role == "rep":
        form.fields.pop("owner", None)


def contact_detail(request, pk):
    contact = get_object_or_404(
        scope_to_user(Contact.objects.select_related("company", "owner"), request.user), pk=pk
    )
    context = {
        "contact": contact,
        "activities": contact.activities.select_related("actor")[:50],
        "open_lead": contact.open_lead,
        "open_tasks": Task.objects.filter(
            lead__contact=contact, done_at__isnull=True
        ).select_related("owner"),
        "task_form": TaskForm(),
    }
    return render(request, "pipeline/contact_detail.html", context)


def contact_export(request, pk):
    """GDPR data export — JSON of the contact + everything linked (BACKLOG 4.6)."""
    contact = get_object_or_404(
        scope_to_user(Contact.objects.select_related("company", "owner"), request.user), pk=pk
    )
    response = JsonResponse(contact_export_data(contact), json_dumps_params={"indent": 2})
    response["Content-Disposition"] = f'attachment; filename="contact-{contact.pk}-export.json"'
    return response


@require_POST
def contact_delete(request, pk):
    """GDPR delete — cascades leads, activities, enrollments, messages, tasks."""
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    email = contact.email
    contact.delete()
    response = hx_toast(f"Contact {email} deleted.")
    response["HX-Redirect"] = reverse("contacts")
    return response


def contact_timeline(request, pk):
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    activities = contact.activities.select_related("actor")[:50]
    return render(request, "pipeline/_timeline.html", {"activities": activities})


@require_POST
def contact_note_add(request, pk):
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    text = request.POST.get("text", "").strip()
    if not text:
        return hx_toast("Note cannot be empty.", level="error")
    lead = contact.open_lead
    Activity.objects.create(
        contact=contact,
        lead=lead,
        type=Activity.Type.NOTE,
        actor=request.user,
        payload={"text": text},
    )
    if lead:
        lead.last_activity_at = timezone.now()
        lead.save(update_fields=["last_activity_at", "updated_at"])
    return hx_toast("Note added.", extra_events={"refresh-timeline": True})


@require_POST
def contact_lead_create(request, pk):
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    if contact.open_lead:
        return hx_toast("Contact already has an open lead.", level="error")
    create_open_lead(contact, owner=request.user if request.user.role == "rep" else None)
    response = hx_toast("Lead created.")
    response["HX-Refresh"] = "true"
    return response


@require_POST
def contact_task_create(request, pk):
    contact = get_object_or_404(scope_to_user(Contact.objects, request.user), pk=pk)
    lead = contact.open_lead
    if not lead:
        return hx_toast("Create a lead first — tasks attach to leads.", level="error")
    form = TaskForm(request.POST)
    if not form.is_valid():
        return hx_toast("Title and due date are required.", level="error")
    task = form.save(commit=False)
    task.lead = lead
    task.owner = request.user
    task.save()
    response = hx_toast("Task created.")
    response["HX-Refresh"] = "true"
    return response


# ---------------------------------------------------------------- companies


def companies_list(request):
    qs = Company.objects.annotate(num_contacts=Count("contacts"))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(domain__icontains=q))
    page = Paginator(qs.order_by("name"), 25).get_page(request.GET.get("page"))
    context = {"page": page, "target": "#companies-table"}
    template = "pipeline/_companies_table.html" if request.htmx else "pipeline/companies_list.html"
    return render(request, template, context)


def company_create(request):
    return _company_form(request, None)


def company_edit(request, pk):
    return _company_form(request, get_object_or_404(Company, pk=pk))


def _company_form(request, company):
    if request.method == "POST":
        form = CompanyForm(request.POST, instance=company)
        if form.is_valid():
            form.save()
            return hx_toast(
                f"Company {'updated' if company else 'created'}.",
                extra_events={"close-modal": True, "refresh-companies": True},
            )
    else:
        form = CompanyForm(instance=company)
    action = reverse("company-edit", args=[company.pk]) if company else reverse("company-create")
    title = "Edit company" if company else "New company"
    return render(
        request, "pipeline/_modal_form.html", {"form": form, "modal_title": title, "action": action}
    )


def company_detail(request, pk):
    company = get_object_or_404(Company, pk=pk)
    contacts = scope_to_user(company.contacts.select_related("owner"), request.user).order_by(
        "-created_at"
    )
    return render(
        request, "pipeline/company_detail.html", {"company": company, "contacts": contacts}
    )


# ---------------------------------------------------------------- board & leads


def board(request):
    leads = scope_to_user(Lead.objects.select_related("contact__company", "stage"), request.user)
    if owner := request.GET.get("owner"):
        leads = leads.filter(owner_id=owner)
    leads = list(leads)
    stage_changes = dict(
        Activity.objects.filter(lead__in=leads, type=Activity.Type.STAGE_CHANGE)
        .values("lead")
        .annotate(m=Max("ts"))
        .values_list("lead", "m")
    )
    for lead in leads:
        lead.stage_since = stage_changes.get(lead.pk, lead.created_at)
    columns = [
        {"stage": stage, "leads": [lead for lead in leads if lead.stage_id == stage.pk]}
        for stage in Stage.objects.all()
    ]
    context = {"columns": columns, "owners": _owner_choices(request.user)}
    template = "pipeline/_board.html" if request.htmx else "pipeline/board.html"
    return render(request, template, context)


@require_POST
def lead_move(request, pk):
    lead = get_object_or_404(
        scope_to_user(Lead.objects.select_related("contact"), request.user), pk=pk
    )
    stage = get_object_or_404(Stage, pk=request.POST.get("stage"))
    if stage.pk == lead.stage_id:
        return HttpResponse(status=204)
    lead.move_to(stage, actor=request.user)
    return hx_toast(
        f"{lead.contact.full_name} → {stage.name}", extra_events={"refresh-board": True}
    )


def leads_list(request):
    qs = scope_to_user(
        Lead.objects.select_related("contact__company", "stage", "owner"), request.user
    )
    if stage := request.GET.get("stage"):
        qs = qs.filter(stage_id=stage)
    if status := request.GET.get("status"):
        qs = qs.filter(status=status)
    if owner := request.GET.get("owner"):
        qs = qs.filter(owner_id=owner)
    if stale := request.GET.get("stale"):
        cutoff = timezone.now() - timezone.timedelta(days=int(stale))
        qs = qs.filter(Q(last_activity_at__lt=cutoff) | Q(last_activity_at__isnull=True))
    qs = qs.order_by(F("last_activity_at").desc(nulls_last=True))
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "target": "#leads-table",
        "stages": Stage.objects.all(),
        "owners": _owner_choices(request.user),
    }
    template = "pipeline/_leads_table.html" if request.htmx else "pipeline/leads_list.html"
    return render(request, template, context)


# ---------------------------------------------------------------- tasks


def tasks_list(request):
    qs = Task.objects.filter(owner=request.user).select_related("lead__contact")
    now = timezone.now()
    context = {
        "overdue": qs.filter(done_at__isnull=True, due_at__lt=now),
        "upcoming": qs.filter(done_at__isnull=True, due_at__gte=now),
        "done": qs.filter(done_at__isnull=False).order_by("-done_at")[:10],
    }
    template = "pipeline/_tasks.html" if request.htmx else "pipeline/tasks_list.html"
    return render(request, template, context)


@require_POST
def task_toggle(request, pk):
    task = get_object_or_404(
        Task.objects.select_related("lead__contact"), pk=pk, owner=request.user
    )
    if task.done_at:
        task.done_at = None
        msg = "Task reopened."
    else:
        task.done_at = timezone.now()
        Activity.objects.create(
            contact=task.lead.contact,
            lead=task.lead,
            type=Activity.Type.TASK_DONE,
            actor=request.user,
            payload={"title": task.title},
        )
        msg = "Task done."
    task.save(update_fields=["done_at", "updated_at"])
    return hx_toast(msg, extra_events={"refresh-tasks": True, "refresh-timeline": True})


# ---------------------------------------------------------------- stage settings


def _stages_response(request, toast_msg=None, level="success"):
    response = render(
        request,
        "pipeline/_stages.html",
        {"stages": Stage.objects.annotate(lead_count=Count("leads"))},
    )
    if toast_msg:
        response["HX-Trigger"] = json.dumps({"toast": {"level": level, "msg": toast_msg}})
    return response


@role_required("admin")
def stages_settings(request):
    return render(
        request,
        "pipeline/stages_settings.html",
        {"stages": Stage.objects.annotate(lead_count=Count("leads"))},
    )


@role_required("admin")
@require_POST
def stage_add(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return _stages_response(request, "Stage name is required.", level="error")
    max_order = Stage.objects.aggregate(m=Max("order"))["m"] or 0
    stage = Stage(name=name, order=max_order + 1)
    try:
        stage.full_clean()
        stage.save()
    except ValidationError as exc:
        return _stages_response(request, "; ".join(exc.messages), level="error")
    return _stages_response(request, f"Stage “{name}” added.")


@role_required("admin")
@require_POST
def stage_rename(request, pk):
    stage = get_object_or_404(Stage, pk=pk)
    name = request.POST.get("name", "").strip()
    if not name:
        return _stages_response(request, "Stage name is required.", level="error")
    stage.name = name
    stage.save(update_fields=["name", "updated_at"])
    return _stages_response(request, "Stage renamed.")


@role_required("admin")
@require_POST
def stage_move(request, pk):
    stage = get_object_or_404(Stage, pk=pk)
    if request.POST.get("dir") == "up":
        neighbor = Stage.objects.filter(order__lt=stage.order).order_by("-order").first()
    else:
        neighbor = Stage.objects.filter(order__gt=stage.order).order_by("order").first()
    if neighbor:
        stage.order, neighbor.order = neighbor.order, stage.order
        Stage.objects.bulk_update([stage, neighbor], ["order"])
    return _stages_response(request)


@role_required("admin")
def duplicate_companies(request):
    context = {"groups": find_duplicate_groups()}
    template = "pipeline/_duplicates.html" if request.htmx else "pipeline/duplicates.html"
    return render(request, template, context)


@role_required("admin")
@require_POST
def company_merge(request):
    primary = get_object_or_404(Company, pk=request.POST.get("primary"))
    duplicates = list(
        Company.objects.filter(pk__in=request.POST.getlist("dup")).exclude(pk=primary.pk)
    )
    if not duplicates:
        return hx_toast("Nothing to merge.", level="error")
    moved = merge_companies(primary, duplicates, actor=request.user)
    word = "company" if len(duplicates) == 1 else "companies"
    return hx_toast(
        f"Merged {len(duplicates)} {word} into {primary.name} — {moved} contact(s) moved.",
        extra_events={"refresh-duplicates": True},
    )


@role_required("admin")
@require_POST
def stage_delete(request, pk):
    stage = get_object_or_404(Stage, pk=pk)
    if stage.is_won or stage.is_lost:
        return _stages_response(request, "Won/Lost stages cannot be deleted.", level="error")
    lead_count = stage.leads.count()
    if lead_count:
        return _stages_response(
            request,
            f"“{stage.name}” has {lead_count} lead(s) — move them to another stage first.",
            level="error",
        )
    stage.delete()
    return _stages_response(request, f"Stage “{stage.name}” deleted.")
