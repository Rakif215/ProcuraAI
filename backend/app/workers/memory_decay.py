"""
app/workers/memory_decay.py
----------------------------
Celery task: daily memory maintenance — decay stale memories.
"""
import logging
from app.workers.celery_app import celery_app
from app.services.memory_service import decay_stale_memories

logger = logging.getLogger(__name__)


@celery_app.task(name="memory.decay_stale", bind=True, max_retries=1)
def run_memory_decay(self):
    """Daily task: decay confidence on memories not seen in 30+ days."""
    logger.info("Running daily memory decay")
    count = decay_stale_memories()
    logger.info("Memory decay complete: %d memories updated", count)
    return {"updated": count}
