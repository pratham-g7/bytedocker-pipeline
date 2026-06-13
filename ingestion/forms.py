from django import forms

from outreach.models import Mailbox, Sequence
from pipeline.forms import StyledModelForm

from .models import IntakeSource


class IntakeSourceForm(StyledModelForm):
    class Meta:
        model = IntakeSource
        fields = ["name", "slug", "auto_enroll", "mailbox", "is_active"]
        help_texts = {
            "slug": "Used in the form URL and webhook attribution (lowercase, no spaces).",
            "auto_enroll": "Optional — new contacts are enrolled into this sequence.",
            "mailbox": "Required for auto-enroll: the sending identity.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["auto_enroll"].queryset = Sequence.objects.filter(is_active=True).order_by(
            "name"
        )
        self.fields["auto_enroll"].required = False
        self.fields["mailbox"].queryset = Mailbox.objects.filter(
            status=Mailbox.Status.ACTIVE
        ).order_by("email")
        self.fields["mailbox"].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("auto_enroll") and not cleaned.get("mailbox"):
            self.add_error("mailbox", "Pick a sending mailbox to auto-enroll.")
        return cleaned


class CaptureForm(forms.Form):
    """The public hosted-form fields (a flat dict for intake_contact)."""

    email = forms.EmailField()
    first_name = forms.CharField(max_length=80, required=False)
    last_name = forms.CharField(max_length=80, required=False)
    company = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
