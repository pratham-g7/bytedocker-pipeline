from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.http import hx_toast

from .forms import EmailTemplateForm
from .models import EmailTemplate
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
