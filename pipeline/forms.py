from django import forms

from .models import Company, Contact, Task


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")


class ContactForm(StyledModelForm):
    class Meta:
        model = Contact
        fields = [
            "first_name",
            "last_name",
            "email",
            "title",
            "phone",
            "linkedin_url",
            "company",
            "owner",
            "source",
        ]


class CompanyForm(StyledModelForm):
    class Meta:
        model = Company
        fields = ["name", "domain", "industry", "size", "location"]


class TaskForm(StyledModelForm):
    class Meta:
        model = Task
        fields = ["title", "due_at"]
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}
