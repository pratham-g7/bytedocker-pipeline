"""Outreach models (DATA_SPEC §3) + Enrollment/Message state machines (§4).

Status fields are only ever written through the state-machine methods on
Enrollment — no raw status writes (BACKLOG 2.1 AC). OAuth tokens are stored
Fernet-encrypted via the Mailbox.token property, never in plaintext.
"""

import uuid
from datetime import time

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.crypto import decrypt, encrypt
from core.models import TimeStampedModel
from pipeline.models import Activity, Contact

from .rendering import derive_body_text, validate_merge_fields


class InvalidTransition(Exception):
    """Raised on a state-machine transition the spec forbids (DATA_SPEC §4)."""


class Mailbox(TimeStampedModel):
    class Provider(models.TextChoices):
        GMAIL = "gmail", "Gmail"
        OUTLOOK = "outlook", "Outlook"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        ERROR = "error", "Error"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mailboxes"
    )
    provider = models.CharField(max_length=10, choices=Provider.choices)
    email = models.EmailField(unique=True)
    oauth_token = models.TextField(blank=True)  # Fernet-encrypted JSON — use .token
    history_cursor = models.CharField(max_length=64, blank=True)
    daily_cap = models.PositiveIntegerField(default=100)
    sends_today = models.PositiveIntegerField(default=0)
    send_window_start = models.TimeField(default=time(8, 0))
    send_window_end = models.TimeField(default=time(18, 0))
    timezone = models.CharField(max_length=40, default="UTC")  # IANA name
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name_plural = "mailboxes"

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def token(self) -> str:
        """Decrypted OAuth token JSON; empty string when not connected."""
        return decrypt(self.oauth_token) if self.oauth_token else ""

    @token.setter
    def token(self, value: str) -> None:
        self.oauth_token = encrypt(value) if value else ""

    def __str__(self):
        return self.email


class EmailTemplate(TimeStampedModel):
    name = models.CharField(max_length=120)
    subject = models.CharField(max_length=300)
    body_html = models.TextField()
    body_text = models.TextField(blank=True)

    def clean(self):
        for source in (self.subject, self.body_html, self.body_text):
            validate_merge_fields(source)

    def save(self, *args, **kwargs):
        if self.body_html and not self.body_text:
            self.body_text = derive_body_text(self.body_html)
        # Enforced at save, not just in forms: a template with an unknown merge
        # field must never exist to break a send (ENGINE_SPEC §6).
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Sequence(TimeStampedModel):
    name = models.CharField(max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sequences"
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class SequenceStep(TimeStampedModel):
    sequence = models.ForeignKey(Sequence, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    wait_days = models.PositiveSmallIntegerField()  # days after previous step (step 1: enroll)
    template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT, related_name="steps")

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["sequence", "order"], name="unique_step_order")
        ]

    def __str__(self):
        return f"{self.sequence} · step {self.order}"


class Enrollment(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        REPLIED = "replied", "Replied"
        BOUNCED = "bounced", "Bounced"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        FINISHED = "finished", "Finished"

    TERMINAL_STATUSES = frozenset(
        {Status.REPLIED, Status.BOUNCED, Status.UNSUBSCRIBED, Status.FINISHED}
    )

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="enrollments")
    sequence = models.ForeignKey(Sequence, on_delete=models.CASCADE, related_name="enrollments")
    mailbox = models.ForeignKey(  # sending identity is fixed at enrollment
        Mailbox, on_delete=models.PROTECT, related_name="enrollments"
    )
    current_step = models.PositiveSmallIntegerField(default=0)  # 0 = step 1 not yet sent
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    next_send_at = models.DateTimeField(null=True, blank=True)
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="enrollments_created"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "sequence"],
                condition=models.Q(status__in=["active", "paused"]),
                name="one_live_enrollment_per_contact_sequence",
            )
        ]
        # Hot index: the dispatcher's due-enrollments query rides this (ENGINE_SPEC §1).
        indexes = [models.Index(fields=["status", "next_send_at"], name="outreach_enr_due_idx")]

    # -- state machine (DATA_SPEC §4) — the only writers of `status` ----------

    def _require(self, *allowed: str) -> None:
        if self.status not in allowed:
            raise InvalidTransition(
                f"Cannot transition enrollment {self.pk} from {self.status!r} "
                f"(allowed from: {', '.join(allowed)})."
            )

    def pause(self):
        self._require(self.Status.ACTIVE)
        self.status = self.Status.PAUSED
        self.save(update_fields=["status", "updated_at"])

    def resume(self):
        # Terminal states never resume; re-engaging means a new enrollment.
        self._require(self.Status.PAUSED)
        self.status = self.Status.ACTIVE
        if self.next_send_at is None:
            self.next_send_at = timezone.now()
        self.save(update_fields=["status", "next_send_at", "updated_at"])

    def advance(self, step_no: int, next_send_at):
        """Happy path: step `step_no` was sent; schedule the next one."""
        self._require(self.Status.ACTIVE)
        self.current_step = step_no
        self.next_send_at = next_send_at
        self.save(update_fields=["current_step", "next_send_at", "updated_at"])

    def mark_replied(self, payload: dict | None = None):
        self._require(self.Status.ACTIVE, self.Status.PAUSED)
        self._terminalize(self.Status.REPLIED)
        self._log(Activity.Type.EMAIL_REPLIED, payload)

    def mark_bounced(self, payload: dict | None = None):
        self._require(self.Status.ACTIVE, self.Status.PAUSED)
        self._terminalize(self.Status.BOUNCED)
        if self.contact.bounced_at is None:
            self.contact.bounced_at = timezone.now()
            self.contact.save(update_fields=["bounced_at", "updated_at"])
        self._log(Activity.Type.EMAIL_BOUNCED, payload)

    def mark_unsubscribed(self, payload: dict | None = None):
        self._require(self.Status.ACTIVE, self.Status.PAUSED)
        self._terminalize(self.Status.UNSUBSCRIBED)
        self._log(Activity.Type.UNSUBSCRIBED, payload)

    def mark_finished(self):
        self._require(self.Status.ACTIVE)
        self._terminalize(self.Status.FINISHED)

    def _terminalize(self, status: str) -> None:
        self.status = status
        self.next_send_at = None
        self.save(update_fields=["status", "next_send_at", "updated_at"])

    def _log(self, activity_type: str, payload: dict | None) -> None:
        Activity.objects.create(
            contact=self.contact,
            lead=self.contact.open_lead,
            type=activity_type,
            payload=payload or {},
        )

    def __str__(self):
        return f"{self.contact} → {self.sequence} ({self.status})"


class Message(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        SENT = "sent", "Sent"
        BOUNCED = "bounced", "Bounced"
        FAILED = "failed", "Failed"

    # Public identifier for tracking endpoints — the pk is never exposed.
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="messages")
    step = models.ForeignKey(SequenceStep, on_delete=models.PROTECT, related_name="messages")
    mailbox = models.ForeignKey(Mailbox, on_delete=models.PROTECT, related_name="messages")
    provider_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    thread_id = models.CharField(max_length=120, blank=True, db_index=True)
    subject_rendered = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    # First-event-wins timestamps; repeat events only append Activities (DATA_SPEC §3).
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.enrollment} · step {self.step.order} ({self.status})"
