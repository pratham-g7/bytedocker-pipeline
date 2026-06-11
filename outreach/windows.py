"""Send-window math, evaluated in the mailbox's local timezone (ENGINE_SPEC §1/§7).

Timestamps are stored UTC; every comparison here converts into the mailbox's
IANA tz first. Weekday-only sending is the v1 default (SEND_WEEKDAYS_ONLY flag).
"""

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

JITTER_SECONDS = 90


def _tz(mailbox) -> ZoneInfo:
    return ZoneInfo(mailbox.timezone or "UTC")


def _weekdays_only() -> bool:
    return getattr(settings, "SEND_WEEKDAYS_ONLY", True)


def within_send_window(mailbox, now=None) -> bool:
    local = (now or timezone.now()).astimezone(_tz(mailbox))
    if _weekdays_only() and local.weekday() >= 5:
        return False
    return mailbox.send_window_start <= local.time() < mailbox.send_window_end


def window_open(mailbox, now=None) -> datetime:
    """Earliest moment >= now the window is open (UTC). Now, if it's open now."""
    local = (now or timezone.now()).astimezone(_tz(mailbox))
    for offset in range(8):  # always reaches a weekday within a week
        day = (local + timedelta(days=offset)).date()
        if _weekdays_only() and day.weekday() >= 5:
            continue
        start = datetime.combine(day, mailbox.send_window_start, tzinfo=_tz(mailbox))
        end = datetime.combine(day, mailbox.send_window_end, tzinfo=_tz(mailbox))
        candidate = max(start, local)
        if candidate < end:
            return candidate.astimezone(UTC)
    return local.astimezone(UTC)  # degenerate window config — send now rather than never


def next_window_open(mailbox, now=None) -> datetime:
    """The next window open strictly after today's — used when the cap is spent."""
    local = (now or timezone.now()).astimezone(_tz(mailbox))
    end_today = datetime.combine(local.date(), mailbox.send_window_end, tzinfo=_tz(mailbox))
    return window_open(mailbox, max(local, end_today).astimezone(UTC))


def jitter() -> timedelta:
    """±90 s so send timing isn't metronomic (ENGINE_SPEC §7)."""
    return timedelta(seconds=random.uniform(-JITTER_SECONDS, JITTER_SECONDS))


def positive_jitter() -> timedelta:
    """Forward-only jitter for deferrals — never lands before the window opens."""
    return timedelta(seconds=random.uniform(0, JITTER_SECONDS))
