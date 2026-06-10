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
    errored = models.PositiveIntegerField(default=0)
    errors_csv = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def source(self) -> str:
        return f"csv:{self.source_label or self.filename}"

    def __str__(self):
        return f"{self.filename} ({self.status})"
