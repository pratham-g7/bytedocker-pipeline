from django import forms

from pipeline.forms import StyledModelForm

from .models import EmailTemplate


class EmailTemplateForm(StyledModelForm):
    class Meta:
        model = EmailTemplate
        fields = ["name", "subject", "body_html", "body_text"]
        widgets = {
            "body_html": forms.Textarea(attrs={"rows": 12, "class": "input font-mono text-xs"}),
            "body_text": forms.Textarea(attrs={"rows": 5, "class": "input font-mono text-xs"}),
        }
        labels = {"body_html": "Body (HTML)", "body_text": "Body (plain text)"}
        help_texts = {"body_text": "Leave blank to derive automatically from the HTML body."}
