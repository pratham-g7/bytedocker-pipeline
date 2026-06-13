from datetime import UTC, datetime, time, timedelta

import pytest
from django.utils import timezone

from outreach.models import Enrollment, Mailbox
from outreach.tasks import dispatch_due_sends

from .factories import EnrollmentFactory, MailboxFactory, SequenceStepFactory

pytestmark = pytest.mark.django_db


def _aged_mailbox(days_old, **kwargs):
    """A mailbox whose created_at is `days_old` days in the past."""
    mailbox = MailboxFactory(**kwargs)
    Mailbox.objects.filter(pk=mailbox.pk).update(
        created_at=timezone.now() - timedelta(days=days_old)
    )
    mailbox.refresh_from_db()
    return mailbox


# ---------------------------------------------------------------- effective cap


def test_cap_ramps_one_step_per_day(settings):
    settings.MAILBOX_WARMUP = True
    settings.MAILBOX_WARMUP_STEP = 20
    assert _aged_mailbox(0, daily_cap=100).effective_cap() == 20  # first day
    assert _aged_mailbox(1, daily_cap=100).effective_cap() == 40
    assert _aged_mailbox(3, daily_cap=100).effective_cap() == 80


def test_cap_never_exceeds_configured(settings):
    settings.MAILBOX_WARMUP = True
    settings.MAILBOX_WARMUP_STEP = 20
    assert _aged_mailbox(20, daily_cap=100).effective_cap() == 100  # 20*21 capped to 100


def test_per_mailbox_warmup_off_uses_full_cap(settings):
    settings.MAILBOX_WARMUP = True
    assert _aged_mailbox(0, daily_cap=100, warmup=False).effective_cap() == 100


def test_global_warmup_off_uses_full_cap(settings):
    settings.MAILBOX_WARMUP = False
    assert _aged_mailbox(0, daily_cap=100, warmup=True).effective_cap() == 100


# ---------------------------------------------------------------- dispatcher


def test_dispatcher_honors_warmup_cap(settings, monkeypatch):
    settings.MAILBOX_WARMUP = True
    settings.MAILBOX_WARMUP_STEP = 20
    sent = []
    provider = type("P", (), {"send": lambda self, **kw: sent.append(kw) or ("m", "t")})()
    monkeypatch.setattr("outreach.tasks.get_provider", lambda mb: provider)

    # brand-new mailbox: effective cap 20, already at it → defer, no send
    mailbox = _aged_mailbox(
        0,
        daily_cap=100,
        sends_today=20,
        timezone="UTC",
        send_window_start=time(0, 0),
        send_window_end=time(23, 59),
    )
    enrollment = EnrollmentFactory(mailbox=mailbox, next_send_at=timezone.now())
    SequenceStepFactory(sequence=enrollment.sequence, order=1, wait_days=0)

    dispatch_due_sends(now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))

    assert sent == []  # warmup cap reached even though daily_cap is 100
    enrollment.refresh_from_db()
    assert enrollment.status == Enrollment.Status.ACTIVE  # deferred, not terminal
    assert enrollment.next_send_at > datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def test_current_cap_property_matches():
    mailbox = _aged_mailbox(0, daily_cap=100)
    assert mailbox.current_cap == mailbox.effective_cap()
