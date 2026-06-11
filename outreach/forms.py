from zoneinfo import ZoneInfo

from django import forms

from pipeline.forms import StyledModelForm

from .models import EmailTemplate, Mailbox, Sequence, SequenceStep, scope_sequences


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


class SequenceForm(StyledModelForm):
    class Meta:
        model = Sequence
        fields = ["name", "is_active"]


class StepForm(StyledModelForm):
    class Meta:
        model = SequenceStep
        fields = ["template", "wait_days"]
        help_texts = {"wait_days": "Days after the previous step (first step: after enrollment)."}


class EnrollForm(forms.Form):
    """Enroll modal: sequence + sending mailbox (UI_SPEC §3 contact detail)."""

    sequence = forms.ModelChoiceField(queryset=Sequence.objects.none())
    mailbox = forms.ModelChoiceField(queryset=Mailbox.objects.none(), label="Send from")

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sequence"].queryset = scope_sequences(
            Sequence.objects.filter(is_active=True).order_by("name"), user
        )
        mailboxes = user.mailboxes.filter(status=Mailbox.Status.ACTIVE).order_by("email")
        self.fields["mailbox"].queryset = mailboxes
        first = mailboxes.first()  # default = owner's first active mailbox
        if first:
            self.fields["mailbox"].initial = first.pk
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
