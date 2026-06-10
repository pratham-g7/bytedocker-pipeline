from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


def normalize_domain(value: str) -> str:
    """'https://www.Acme.com/about' -> 'acme.com' (DATA_SPEC §5)."""
    value = value.strip().lower()
    if "//" in value:
        value = urlparse(value).netloc or value.split("//", 1)[1]
    value = value.split("/", 1)[0]
    return value.removeprefix("www.")


class Company(TimeStampedModel):
    name = models.CharField(max_length=200)
    domain = models.CharField(max_length=120, unique=True, null=True, blank=True)
    industry = models.CharField(max_length=80, blank=True)
    size = models.CharField(max_length=40, blank=True)
    location = models.CharField(max_length=120, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "companies"

    def save(self, *args, **kwargs):
        self.domain = normalize_domain(self.domain) if self.domain else None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Contact(TimeStampedModel):
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts"
    )
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(unique=True)
    title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    linkedin_url = models.URLField(blank=True)
    source = models.CharField(max_length=60, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contacts",
    )
    custom_fields = models.JSONField(default=dict, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "created_at"])]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email

    @property
    def open_lead(self):
        return self.leads.filter(status=Lead.Status.OPEN).first()

    def __str__(self):
        return self.full_name


class Stage(TimeStampedModel):
    name = models.CharField(max_length=60)
    order = models.PositiveSmallIntegerField()
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]

    def clean(self):
        if self.is_won and self.is_lost:
            raise ValidationError("A stage cannot be both won and lost.")
        for flag in ("is_won", "is_lost"):
            if getattr(self, flag):
                clash = Stage.objects.filter(**{flag: True}).exclude(pk=self.pk)
                if clash.exists():
                    raise ValidationError(f"Only one stage may have {flag}.")

    def __str__(self):
        return self.name


class Lead(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="leads")
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="leads")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="leads",
    )
    source = models.CharField(max_length=60, blank=True)
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contact"],
                condition=models.Q(status="open"),
                name="one_open_lead_per_contact",
            )
        ]

    def move_to(self, stage: Stage, actor=None):
        old = self.stage
        self.stage = stage
        if stage.is_won:
            self.status = self.Status.WON
        elif stage.is_lost:
            self.status = self.Status.LOST
        else:
            self.status = self.Status.OPEN
        self.last_activity_at = timezone.now()
        self.save(update_fields=["stage", "status", "last_activity_at", "updated_at"])
        Activity.objects.create(
            contact=self.contact,
            lead=self,
            type=Activity.Type.STAGE_CHANGE,
            actor=actor,
            payload={"from": old.name, "to": stage.name},
        )

    def __str__(self):
        return f"{self.contact} ({self.stage})"


def create_open_lead(contact: Contact, owner=None) -> Lead:
    first_stage = Stage.objects.exclude(is_won=True).exclude(is_lost=True).first()
    return Lead.objects.create(
        contact=contact,
        stage=first_stage,
        owner=owner or contact.owner,
        source=contact.source,
        last_activity_at=timezone.now(),
    )


class Activity(models.Model):
    class Type(models.TextChoices):
        EMAIL_SENT = "email_sent", "Email sent"
        EMAIL_OPENED = "email_opened", "Email opened"
        EMAIL_CLICKED = "email_clicked", "Email clicked"
        EMAIL_REPLIED = "email_replied", "Email replied"
        EMAIL_BOUNCED = "email_bounced", "Email bounced"
        NOTE = "note", "Note"
        CALL = "call", "Call"
        STAGE_CHANGE = "stage_change", "Stage change"
        TASK_DONE = "task_done", "Task done"
        ENROLLED = "enrolled", "Enrolled"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        IMPORT = "import", "Import"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="activities")
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="activities"
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    payload = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    ts = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-ts"]
        indexes = [models.Index(fields=["contact", "-ts"])]
        verbose_name_plural = "activities"

    def __str__(self):
        return f"{self.type} · {self.contact}"


class Task(TimeStampedModel):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="tasks")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tasks"
    )
    title = models.CharField(max_length=200)
    due_at = models.DateTimeField()
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_at"]

    def __str__(self):
        return self.title
