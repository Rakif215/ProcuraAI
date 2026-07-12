"""
app/api/v1/memory.py
---------------------
REST API for the user memory layer.

User-facing endpoints (trust is essential — from Perplexity guidance):
  - View their profile and all memories
  - Edit/update stable profile fields
  - Correct or delete individual memories
  - Submit feedback on agent outputs
  - View memory evidence (audit trail)

This transparency is critical for user trust.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.deps import AuthUser
from app.db.client import supabase
from app.services.memory_service import record_feedback, record_correction
from app.services.telemetry import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    role: Optional[str] = None
    industry: Optional[str] = None
    timezone: Optional[str] = None
    language: Optional[str] = None
    tone: Optional[str] = None
    email_length: Optional[str] = None
    preferences: Optional[dict] = None


class MemoryCreate(BaseModel):
    category: str
    content: str
    memory_key: Optional[str] = None
    memory_value: Optional[str] = None
    scope: Optional[str] = "general"


class MemoryCorrection(BaseModel):
    correction: str


class FeedbackCreate(BaseModel):
    feedback_type: str   # "accepted" | "edited" | "rejected" | "corrected"
    source_type: str     # "email_draft" | "chat_response" | "suggested_reply"
    source_id: str
    memory_id: Optional[str] = None
    correction: Optional[str] = None


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile(user: AuthUser):
    """Get the current user's stable profile."""
    try:
        result = (
            supabase.table("user_profiles")
            .select("*")
            .eq("user_id", user.user_id)
            .single()
            .execute()
        )
        if not result.data:
            # Return empty profile (will be created on first update)
            return {
                "user_id": user.user_id,
                "role": None,
                "industry": None,
                "timezone": "UTC",
                "language": "en",
                "tone": "professional",
                "email_length": "medium",
                "preferences": {},
            }
        return result.data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/profile")
async def update_profile(body: ProfileUpdate, user: AuthUser):
    """
    Update the user's stable profile.
    This immediately affects how the agent behaves on next conversation.
    """
    from datetime import datetime, timezone

    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["user_id"] = user.user_id
    update_data["tenant_id"] = user.tenant_id
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        result = supabase.table("user_profiles").upsert(
            update_data,
            on_conflict="user_id",
        ).execute()
        return result.data[0] if result.data else update_data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Memory Items ──────────────────────────────────────────────────────────────

@router.get("/items")
async def list_memories(
    user: AuthUser,
    category: Optional[str] = None,
    status: Optional[str] = None,
    scope: Optional[str] = None,
):
    """
    List all memories for this user.
    Supports filtering by category, status, and scope.
    """
    try:
        query = (
            supabase.table("memory_items")
            .select("id, category, content, memory_key, memory_value, scope, source, confidence, status, evidence_count, last_seen_at, created_at")
            .eq("user_id", user.user_id)
            .not_.in_("status", ["deleted"])
            .order("confidence", desc=True)
            .order("created_at", desc=True)
        )
        if category:
            query = query.eq("category", category)
        if status:
            query = query.eq("status", status)
        if scope:
            query = query.eq("scope", scope)

        result = query.execute()
        return result.data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_memory(body: MemoryCreate, user: AuthUser):
    """
    Manually create a memory (explicit user instruction).
    These are saved as 'active' with high confidence (0.95).
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    try:
        result = supabase.table("memory_items").insert({
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "category": body.category,
            "content": body.content,
            "memory_key": body.memory_key,
            "memory_value": body.memory_value,
            "scope": body.scope or "general",
            "source": "explicit",
            "confidence": 0.95,
            "status": "active",
            "evidence_count": 1,
            "last_seen_at": now,
        }).execute()

        memory_id = result.data[0]["id"]

        # Create evidence for manual creation
        supabase.table("memory_evidence").insert({
            "memory_id": memory_id,
            "source_type": "explicit_command",
            "extraction_type": "explicit_statement",
            "span": body.content[:200],
        }).execute()

        return result.data[0]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/items/{memory_id}/correct")
async def correct_memory(memory_id: str, body: MemoryCorrection, user: AuthUser):
    """
    Correct an existing memory. Marks the old one as 'contradicted' and
    creates a new 'active' memory with the user's correction.
    This is the primary trust control per Perplexity guidance.
    """
    # Verify ownership
    existing = supabase.table("memory_items").select("id").eq("id", memory_id).eq("user_id", user.user_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Memory not found")

    new_id = record_correction(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        old_memory_id=memory_id,
        correction_text=body.correction,
    )
    if not new_id:
        raise HTTPException(status_code=500, detail="Failed to save correction")

    return {"old_memory_id": memory_id, "new_memory_id": new_id, "status": "corrected"}


@router.delete("/items/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: str, user: AuthUser):
    """
    Delete (soft-delete) a memory. Sets status='deleted'.
    User control is essential for trust and GDPR compliance.
    """
    try:
        supabase.table("memory_items").update({
            "status": "deleted"
        }).eq("id", memory_id).eq("user_id", user.user_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/items/{memory_id}/promote")
async def promote_memory(memory_id: str, user: AuthUser):
    """Manually promote a candidate memory to active status."""
    try:
        supabase.table("memory_items").update({
            "status": "active",
        }).eq("id", memory_id).eq("user_id", user.user_id).eq("status", "candidate").execute()
        return {"status": "promoted"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Evidence (audit trail) ────────────────────────────────────────────────────

@router.get("/items/{memory_id}/evidence")
async def get_memory_evidence(memory_id: str, user: AuthUser):
    """
    Get the audit trail for a memory — why it was created and what confirmed it.
    Transparency for user trust.
    """
    # Verify ownership first
    ownership = supabase.table("memory_items").select("id").eq("id", memory_id).eq("user_id", user.user_id).single().execute()
    if not ownership.data:
        raise HTTPException(status_code=404, detail="Memory not found")

    try:
        result = (
            supabase.table("memory_evidence")
            .select("*")
            .eq("memory_id", memory_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Feedback ──────────────────────────────────────────────────────────────────

@router.post("/feedback")
async def submit_feedback(body: FeedbackCreate, user: AuthUser):
    """
    Submit feedback on an agent output (email draft, chat response).
    This adjusts memory confidence and enables implicit learning.
    """
    valid_types = {"accepted", "edited", "rejected", "corrected"}
    if body.feedback_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"feedback_type must be one of {valid_types}")

    record_feedback(
        user_id=user.user_id,
        feedback_type=body.feedback_type,
        source_type=body.source_type,
        source_id=body.source_id,
        memory_id=body.memory_id,
        correction=body.correction,
    )
    log_event(
        "memory_feedback_recorded",
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        properties={
            "feedback_type": body.feedback_type,
            "source_type": body.source_type,
            "source_id": body.source_id,
        },
    )
    return {"status": "recorded"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_memory_stats(user: AuthUser):
    """Get memory statistics — useful for the settings/memory page."""
    try:
        result = (
            supabase.table("memory_items")
            .select("status, category, confidence")
            .eq("user_id", user.user_id)
            .not_.in_("status", ["deleted"])
            .execute()
        )
        items = result.data or []

        by_status = {}
        by_category = {}
        total_confidence = 0.0

        for item in items:
            s = item.get("status", "unknown")
            c = item.get("category", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
            by_category[c] = by_category.get(c, 0) + 1
            total_confidence += item.get("confidence", 0)

        return {
            "total": len(items),
            "active": by_status.get("active", 0),
            "candidates": by_status.get("candidate", 0),
            "by_category": by_category,
            "avg_confidence": round(total_confidence / len(items), 2) if items else 0,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
