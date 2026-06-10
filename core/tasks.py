import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def heartbeat():
    """Beat-scheduled liveness check; proves worker + beat + broker are wired."""
    logger.info("heartbeat: worker and beat are alive")
    return "ok"
