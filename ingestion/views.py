from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.permissions import scope_to_user

from .models import ImportJob
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
