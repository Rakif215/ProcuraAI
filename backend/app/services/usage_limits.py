import logging
from datetime import datetime, time, timezone
from typing import Optional

from app.core.config import settings
from app.db.client import supabase
from app.services.telemetry import log_event

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate for budget guardrails."""
    return max(1, len(text or "") // 4)


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc).isoformat()


def _count_events(event_name: str, tenant_id: Optional[str], user_id: Optional[str] = None) -> int:
    query = (
        supabase.table("telemetry_events")
        .select("id", count="exact")
        .eq("event_name", event_name)
        .gte("created_at", _today_start_iso())
        .limit(1)
    )
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    if user_id:
        query = query.eq("user_id", user_id)
    try:
        return query.execute().count or 0
    except Exception as exc:
        logger.debug("Usage count skipped for %s: %s", event_name, exc)
        return 0


def _sum_estimated_tokens(tenant_id: Optional[str], user_id: Optional[str] = None) -> int:
    query = (
        supabase.table("telemetry_events")
        .select("properties")
        .eq("event_name", "model_call_completed")
        .gte("created_at", _today_start_iso())
        .limit(1000)
    )
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    if user_id:
        query = query.eq("user_id", user_id)
    try:
        rows = query.execute().data or []
        return sum(int((row.get("properties") or {}).get("estimated_tokens") or 0) for row in rows)
    except Exception as exc:
        logger.debug("Token usage sum skipped: %s", exc)
        return 0


def can_use_ai(
    *,
    tenant_id: Optional[str],
    user_id: Optional[str] = None,
    purpose: str,
    estimated_tokens: int = 0,
) -> bool:
    """
    Simple pilot budget guard.
    Uses telemetry rows instead of introducing a new required table before deploy.
    """
    tenant_calls = _count_events("model_call_completed", tenant_id)
    user_calls = _count_events("model_call_completed", tenant_id, user_id) if user_id else 0
    tenant_tokens = _sum_estimated_tokens(tenant_id)
    user_tokens = _sum_estimated_tokens(tenant_id, user_id) if user_id else 0

    if tenant_calls >= settings.ai_daily_calls_per_tenant:
        log_event(
            "model_call_skipped_budget",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"scope": "tenant", "purpose": purpose, "estimated_tokens": estimated_tokens},
        )
        return False
    if user_id and user_calls >= settings.ai_daily_calls_per_user:
        log_event(
            "model_call_skipped_budget",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"scope": "user", "purpose": purpose, "estimated_tokens": estimated_tokens},
        )
        return False
    if tenant_tokens + estimated_tokens >= settings.ai_daily_tokens_per_tenant:
        log_event(
            "model_call_skipped_budget",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"scope": "tenant_tokens", "purpose": purpose, "estimated_tokens": estimated_tokens},
        )
        return False
    if user_id and user_tokens + estimated_tokens >= settings.ai_daily_tokens_per_user:
        log_event(
            "model_call_skipped_budget",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"scope": "user_tokens", "purpose": purpose, "estimated_tokens": estimated_tokens},
        )
        return False
    return True
