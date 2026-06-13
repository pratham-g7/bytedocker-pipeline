import secrets

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class ImportJob(TimeStampedModel):
    """One CSV import run (DATA_SPEC §5.5): audit row + the file itself.

    raw_csv lives in the DB because web and worker are separate containers
    with no shared filesystem.
    """

    class Status(models.TextChoices):
        MAPPING = "mapping", "Awaiting mapping"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="import_jobs"
    )
    filename = models.CharField(max_length=200)
    source_label = models.CharField(max_length=60, blank=True)
    raw_csv = models.TextField()
    mapping = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.MAPPING)
    created_contacts = models.PositiveIntegerField(default=0)
    updated_contacts = models.PositiveIntegerField(default=0)
    created_companies = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    queued = models.PositiveIntegerField(default=0)  # missing-email rows → enrichment queue
    errored = models.PositiveIntegerField(default=0)
    errors_csv = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def source(self) -> str:
        return f"csv:{self.source_label or self.filename}"

    def __str__(self):
        return f"{self.filename} ({self.status})"


class IntakeSource(TimeStampedModel):
    """A lead capture point usable two ways (DATA_SPEC §5, PLAN §5):
    a hosted form at /forms/<slug>/ and a signed webhook at /ingest/webhook/<token>/.

    `token` (opaque URL id) and `secret` (HMAC key) are generated on first save.
    Captured contacts inherit `owner`; if `auto_enroll` + an active `mailbox` are
    set, new contacts are enrolled immediately.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    token = models.CharField(max_length=64, unique=True, blank=True)
    secret = models.CharField(max_length=64, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intake_sources"
    )
    auto_enroll = models.ForeignKey(
        "outreach.Sequence", null=True, blank=True, on_delete=models.SET_NULL
    )
    mailbox = models.ForeignKey(
        "outreach.Mailbox", null=True, blank=True, on_delete=models.SET_NULL
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(24)
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def contact_source(self, channel: str) -> str:
        """Attribution string stored on Contact.source, e.g. 'webhook:landing'."""
        return f"{channel}:{self.slug}"

    def __str__(self):
        return self.name


class EnrichmentTask(TimeStampedModel):
    """A row that arrived without an email (e.g. a LinkedIn export), queued for
    enrichment to resolve into a real Contact (BACKLOG 4.2)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrichment_tasks"
    )
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    title = models.CharField(max_length=120, blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    company_domain = models.CharField(max_length=120, blank=True)
    linkedin_url = models.URLField(blank=True)
    source = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["-created_at"]

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or "(unnamed)"

    def __str__(self):
        return f"{self.display_name} @ {self.company_name or '?'}"
