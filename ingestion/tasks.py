from celery import shared_task

from .models import ImportJob
from .services import execute_import


@shared_task
def run_import_job(job_id: int):
    job = ImportJob.objects.get(pk=job_id)
    try:
        execute_import(job)
    except Exception:
        job.status = ImportJob.Status.FAILED
        job.save(update_fields=["status", "updated_at"])
        raise
