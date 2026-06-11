from zoneinfo import ZoneInfo

from django import forms

from pipeline.forms import StyledModelForm

from .models import EmailTemplate, Mailbox


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


class MailboxSettingsForm(StyledModelForm):
    class Meta:
        model = Mailbox
        fields = ["daily_cap", "send_window_start", "send_window_end", "timezone"]
        widgets = {
            "send_window_start": forms.TimeInput(attrs={"type": "time"}),
            "send_window_end": forms.TimeInput(attrs={"type": "time"}),
        }
        help_texts = {"timezone": "IANA name, e.g. Asia/Kolkata or America/New_York."}

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except Exception:
            raise forms.ValidationError("Unknown IANA timezone name.") from None
        return value
