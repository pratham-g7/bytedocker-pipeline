import hashlib
import hmac
import json

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.http import hx_toast
from core.permissions import role_required, scope_to_user

from .forms import CaptureForm, IntakeSourceForm
from .intake import intake_contact
from .models import ImportJob, IntakeSource
from .services import TARGET_FIELDS, guess_mapping, parse_preview
from .tasks import run_import_job

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _scoped_jobs(request):
    return scope_to_user(ImportJob.objects.select_related("user"), request.user, field="user")


def imports_list(request):
    return render(request, "ingestion/imports_list.html", {"jobs": _scoped_jobs(request)[:50]})


@require_POST
def import_upload(request):
    upload = request.FILES.get("file")
    if not upload:
        return redirect("imports")
    if upload.size > MAX_UPLOAD_BYTES:
        return render(
            request,
            "ingestion/imports_list.html",
            {"jobs": _scoped_jobs(request)[:50], "upload_error": "File too large (max 5 MB)."},
        )
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    job = ImportJob.objects.create(
        user=request.user,
        filename=upload.name,
        source_label=request.POST.get("source_label", "").strip(),
        raw_csv=text,
    )
    return redirect("import-map", pk=job.pk)


def import_map(request, pk):
    job = get_object_or_404(_scoped_jobs(request), pk=pk, status=ImportJob.Status.MAPPING)
    headers, preview = parse_preview(job.raw_csv)
    if request.method == "POST":
        mapping = {}
        for index, header in enumerate(headers):
            target = request.POST.get(f"col_{index}", "")
            if target in TARGET_FIELDS:
                mapping[header] = target
        if "email" not in mapping.values():
            return render(
                request,
                "ingestion/import_map.html",
                _map_context(job, headers, preview, mapping)
                | {"map_error": "Map one column to Email — it's the dedupe key."},
            )
        job.mapping = mapping
        job.save(update_fields=["mapping", "updated_at"])
        run_import_job.delay(job.pk)
        return redirect("import-detail", pk=job.pk)
    return render(request, "ingestion/import_map.html", _map_context(job, headers, preview, None))


def _map_context(job, headers, preview, mapping):
    mapping = mapping if mapping is not None else guess_mapping(headers)
    return {
        "job": job,
        "preview": preview,
        "targets": TARGET_FIELDS,
        "columns": [
            {"index": i, "header": h, "guess": mapping.get(h, "")} for i, h in enumerate(headers)
        ],
    }


def import_detail(request, pk):
    job = get_object_or_404(_scoped_jobs(request), pk=pk)
    template = "ingestion/_job_status.html" if request.htmx else "ingestion/import_detail.html"
    return render(request, template, {"job": job})


def import_errors(request, pk):
    job = get_object_or_404(_scoped_jobs(request), pk=pk)
    response = HttpResponse(job.errors_csv, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="import-{job.pk}-errors.csv"'
    return response


# ---------------------------------------------------------------- intake (public)


@login_not_required
@csrf_exempt
@require_POST
def webhook_intake(request, token):
    """Signed webhook intake (BACKLOG 3.5): HMAC-SHA256 of the body with the
    source secret. Bad signature → 403."""
    source = IntakeSource.objects.filter(token=token, is_active=True).first()
    if source is None:
        raise Http404
    signature = request.headers.get("X-Bytedocker-Signature", "")
    expected = hmac.new(source.secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return HttpResponseForbidden("invalid signature")
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)
    try:
        contact, created = intake_contact(source, "webhook", data)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    status = 201 if created else 200
    return JsonResponse({"ok": True, "created": created, "contact_id": contact.pk}, status=status)


@login_not_required
def capture_form(request, slug):
    """Hosted lead-capture form at /forms/<slug>/ (BACKLOG 3.5)."""
    source = IntakeSource.objects.filter(slug=slug, is_active=True).first()
    if source is None:
        raise Http404
    submitted = False
    form = CaptureForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            intake_contact(source, "form", form.cleaned_data)
            submitted, form = True, CaptureForm()
        except ValueError:
            form.add_error("email", "Please enter a valid email.")
    return render(
        request,
        "ingestion/capture_form.html",
        {"source": source, "form": form, "submitted": submitted},
    )


# ---------------------------------------------------------------- integrations (admin)


@role_required("admin")
def integrations(request):
    context = {
        "sources": IntakeSource.objects.select_related("owner", "auto_enroll", "mailbox"),
        "base_url": settings.BASE_URL.rstrip("/"),
    }
    template = "ingestion/_integrations.html" if request.htmx else "ingestion/integrations.html"
    return render(request, template, context)


@role_required("admin")
def integration_create(request):
    if request.method == "POST":
        form = IntakeSourceForm(request.POST)
        if form.is_valid():
            source = form.save(commit=False)
            source.owner = request.user
            source.save()
            return hx_toast(
                f"Intake source “{source.name}” created.",
                extra_events={"close-modal": True, "refresh-integrations": True},
            )
    else:
        form = IntakeSourceForm()
    return render(
        request,
        "pipeline/_modal_form.html",
        {"form": form, "modal_title": "New intake source", "action": reverse("integration-create")},
    )
