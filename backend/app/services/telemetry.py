import logging
from typing import Any, Optional

from app.db.client import supabase

logger = logging.getLogger(__name__)


def log_event(
    event_name: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    properties: Optional[dict[str, Any]] = None,
) -> None:
    """Best-effort telemetry for Orion Lite pilot behavior."""
    try:
        supabase.table("telemetry_events").insert({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "event_name": event_name,
            "properties": properties or {},
        }).execute()
    except Exception as exc:
        logger.debug("Telemetry skipped for %s: %s", event_name, exc)
