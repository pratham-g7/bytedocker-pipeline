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
}
