"""
app/workers/celery_app.py
--------------------------
Celery application instance and beat schedule configuration.
"""
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "orion",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.email_poller", "app.workers.memory_decay"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Beat schedule — runs email poll every 5 minutes
    beat_schedule={
        "poll-all-email-accounts": {
            "task": "email.poll_all",
            "schedule": 300.0,  # 5 minutes
        },
        "memory-decay-daily": {
            "task": "memory.decay_stale",
            "schedule": 86400.0,  # 24 hours
        },
    },
)
