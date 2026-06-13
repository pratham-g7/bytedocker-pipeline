import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("bytedocker")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# DatabaseScheduler syncs these into django_celery_beat tables on startup.
app.conf.beat_schedule = {
    "core-heartbeat": {
        "task": "core.tasks.heartbeat",
        "schedule": 300.0,
    },
    # Outreach engine (ENGINE_SPEC §5)
    "outreach-dispatch-due-sends": {
        "task": "outreach.tasks.dispatch_due_sends",
        "schedule": 60.0,
    },
    "outreach-reset-daily-counters": {
        "task": "outreach.tasks.reset_daily_counters",
        "schedule": 3600.0,
    },
    "outreach-refresh-expiring-tokens": {
        "task": "outreach.tasks.refresh_expiring_tokens",
        "schedule": 1800.0,
    },
    "outreach-poll-replies": {
        "task": "outreach.tasks.poll_replies",
        "schedule": 180.0,
    },
}
