"""
app/services/memory_service.py
-------------------------------
The Orion memory layer — evidence-backed, lifecycle-managed user memory.

Implements the architecture from Phase 2.5:
  - Retrieve:  get_user_context(user_id)  → compact prompt injection block
  - Extract:   extract_memories(user_id, conversation_turns)  → candidate memories
  - Promote:   promote_candidate(memory_id)  → candidate → active
  - Feedback:  record_feedback(...)  → update confidence from outcomes
  - Decay:     decay_stale_memories()  → lower confidence on unseen memories
  - CRUD:      create / update / delete for user controls

Design rules (from Perplexity guidance):
  1. Only 'active' memories are injected — candidates need repeated confirmation
  2. Explicit statements → save immediately as 'active'
  3. Implicit/inferred → save as 'candidate', promote after 2+ confirmations
  4. Every memory write creates an evidence row
  5. Contradictions set status='contradicted' + contradicted_by FK
  6. Confidence decays if a memory hasn't been seen/confirmed recently
  7. Never inject more than MAX_INJECT memories per prompt (avoid noise)
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.db.client import supabase
from app.services.model_router import chat_completion

logger = logging.getLogger(__name__)

MAX_INJECT = 8           # Max memories injected per prompt
CANDIDATE_THRESHOLD = 2  # Evidence count needed to promote candidate → active
DECAY_AFTER_DAYS = 30    # Start decaying confidence after N days without confirmation
MIN_CONFIDENCE = 0.3     # Delete memories that fall below this


# ── Retrieval ─────────────────────────────────────────────────────────────────

def get_user_context(user_id: str, scope: Optional[str] = None) -> str:
    """
    Build the memory injection block for the system prompt.

    Returns a compact string block like:
        [User Profile]
        Role: founder | Tone: concise | Email length: short
        Timezone: Asia/Qatar | Language: en

        [User Preferences & Memory]
        • preference: Never CC more than 2 people on external emails
        • style: User prefers bullet points over prose in summaries
        • outcome: User consistently shortens suggested replies — keep under 3 sentences

    Only 'active' memories are included. Candidates are excluded from prompts.
    Max MAX_INJECT memories are injected, prioritized by confidence then last_seen_at.
    """
    profile_block = _get_profile_block(user_id)
    memory_block = _get_memory_block(user_id, scope)

    if not profile_block and not memory_block:
        return ""

    parts = []
    if profile_block:
        parts.append(profile_block)
    if memory_block:
        parts.append(memory_block)

    return "\n\n".join(parts)


def _get_profile_block(user_id: str) -> str:
    """Fetch and format the user's stable profile."""
    try:
        result = (
            supabase.table("user_profiles")
            .select("role, industry, timezone, language, tone, email_length, preferences")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        profile = result.data
        if not profile:
            return ""

        lines = ["[User Profile]"]
        parts = []
        if profile.get("role"):
            parts.append(f"Role: {profile['role']}")
        if profile.get("industry"):
            parts.append(f"Industry: {profile['industry']}")
        if profile.get("tone"):
            parts.append(f"Tone: {profile['tone']}")
        if profile.get("email_length"):
            parts.append(f"Email length: {profile['email_length']}")
        if profile.get("timezone"):
            parts.append(f"Timezone: {profile['timezone']}")
        if profile.get("language") and profile["language"] != "en":
            parts.append(f"Language: {profile['language']}")

        lines.append(" | ".join(parts))

        prefs = profile.get("preferences") or {}
        if prefs:
            lines.append("Stable preferences: " + ", ".join(f"{k}: {v}" for k, v in prefs.items()))

        return "\n".join(lines)

    except Exception as exc:
        logger.debug("No user profile for %s: %s", user_id, exc)
        return ""


def _get_memory_block(user_id: str, scope: Optional[str] = None) -> str:
    """Fetch and format active memories for injection."""
    try:
        query = (
            supabase.table("memory_items")
            .select("category, content, confidence, scope, last_seen_at")
            .eq("user_id", user_id)
            .eq("status", "active")
            .order("confidence", desc=True)
            .order("last_seen_at", desc=True)
            .limit(MAX_INJECT)
        )
        if scope:
            # Prefer scope-specific + general memories
            query = query.or_(f"scope.eq.{scope},scope.eq.general,scope.is.null")

        result = query.execute()
        memories = result.data or []

        if not memories:
            return ""

        lines = ["[User Memory — apply these consistently]"]
        for mem in memories:
            icon = {"style": "✍️", "preference": "⚙️", "role": "👤", "outcome": "📊", "fact": "📌"}.get(
                mem.get("category", ""), "•"
            )
            lines.append(f"{icon} {mem['category']}: {mem['content']}")

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to get memory block for %s: %s", user_id, exc)
        return ""


# ── Memory Extraction ─────────────────────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = """\
You are a memory extraction system for an AI assistant. Analyze a conversation and extract \
user preferences, habits, or facts worth remembering for future interactions.

Return ONLY a valid JSON array of memory objects. Each object must have:
  - category: one of "style", "preference", "role", "outcome", "fact"
  - content: human-readable description of the preference/fact (1 sentence)
  - memory_key: machine-friendly key (snake_case, e.g. "email_tone")
  - memory_value: the value (e.g. "concise")
  - scope: context scope ("email_drafting", "scheduling", "communication", "general")
  - source: "explicit" if the user directly stated it, "implicit" if inferred from behavior
  - confidence: 0.0–1.0 (explicit=0.9, implicit=0.6, inferred=0.4)

Rules:
- Only extract memories that would be useful in FUTURE interactions
- Only extract "explicit" (confidence ≥ 0.85) if the user directly said something like \
  "always", "never", "I prefer", "make sure to", "don't", etc.
- Mark anything inferred from behavior as "implicit" (confidence ≤ 0.65)
- Skip trivial, one-off, or session-specific statements
- Return [] if nothing is worth remembering
- Return ONLY the JSON array, no other text"""

_EXTRACTION_USER_TEMPLATE = """\
Conversation to analyze:
{conversation_text}

---
Task: Extract persistent user preferences or facts from the above conversation.
Return a JSON array of memory objects (or [] if nothing is worth storing)."""


def extract_memories(
    user_id: str,
    tenant_id: str,
    conversation_turns: list[dict],
    source_id: str = "",
) -> list[dict]:
    """
    Run the memory extractor on a completed conversation.

    Args:
        user_id:             The user's UUID.
        tenant_id:           The tenant UUID.
        conversation_turns:  List of {"role": "user"|"assistant", "content": "..."} dicts.
        source_id:           conversation_id for evidence linking.

    Returns:
        List of memory_item dicts that were saved (candidates or active).
    """
    if not conversation_turns:
        return []

    # Format conversation for the extractor
    lines = []
    for turn in conversation_turns[-20:]:  # last 20 turns max
        role = turn.get("role", "unknown").upper()
        content = (turn.get("content") or "")[:500]
        lines.append(f"{role}: {content}")

    conversation_text = "\n".join(lines)

    try:
        result = chat_completion(
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": _EXTRACTION_USER_TEMPLATE.format(
                    conversation_text=conversation_text
                )},
            ],
            purpose="memory_extraction",
            tenant_id=tenant_id,
            user_id=user_id,
            temperature=0.1,
            max_tokens=1024,
            json_mode=True,
        )
        if not result:
            return []
        raw = result.content

        # Handle both {"memories": [...]} and direct array responses
        parsed = json.loads(raw)
        candidates = parsed if isinstance(parsed, list) else parsed.get("memories", [])

    except Exception as exc:
        logger.warning("Memory extraction failed: %s", exc)
        return []

    if not candidates:
        return []

    saved = []
    for candidate in candidates[:10]:  # max 10 per conversation
        try:
            memory = _save_candidate_memory(
                user_id=user_id,
                tenant_id=tenant_id,
                candidate=candidate,
                source_id=source_id,
            )
            if memory:
                saved.append(memory)
        except Exception as exc:
            logger.warning("Failed to save candidate memory: %s", exc)

    logger.info("Memory extraction: %d candidates → %d saved for user %s",
                len(candidates), len(saved), user_id)
    return saved


def _save_candidate_memory(
    user_id: str,
    tenant_id: str,
    candidate: dict,
    source_id: str,
) -> Optional[dict]:
    """
    Save a single extracted memory candidate.

    Decision rules:
      - explicit + confidence >= 0.85 → status='active' (save immediately)
      - implicit/inferred → status='candidate' (needs confirmation)
      - If a memory_key already exists → increment evidence_count, potentially promote
    """
    content = (candidate.get("content") or "").strip()
    if not content or len(content) < 10:
        return None

    memory_key = candidate.get("memory_key", "")
    confidence = float(candidate.get("confidence", 0.7))
    source = candidate.get("source", "implicit")

    # Determine initial status
    if source == "explicit" and confidence >= 0.85:
        status = "active"
    else:
        status = "candidate"

    # Check for existing memory with same key (for deduplication)
    existing = None
    if memory_key:
        existing_result = (
            supabase.table("memory_items")
            .select("id, evidence_count, confidence, status")
            .eq("user_id", user_id)
            .eq("memory_key", memory_key)
            .in_("status", ["active", "candidate"])
            .limit(1)
            .execute()
        )
        if existing_result.data:
            existing = existing_result.data[0]

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        # Update: increment evidence count and potentially promote
        new_count = existing["evidence_count"] + 1
        new_confidence = min(1.0, existing["confidence"] + 0.05)  # slight confidence boost
        new_status = existing["status"]

        # Promote candidate → active if seen CANDIDATE_THRESHOLD times
        if existing["status"] == "candidate" and new_count >= CANDIDATE_THRESHOLD:
            new_status = "active"
            logger.info("Promoted memory to active: %s", memory_key)

        supabase.table("memory_items").update({
            "evidence_count": new_count,
            "confidence": new_confidence,
            "status": new_status,
            "last_seen_at": now,
            "content": content,  # update with latest phrasing
        }).eq("id", existing["id"]).execute()

        memory_id = existing["id"]

    else:
        # Insert new memory
        row = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "category": candidate.get("category", "preference"),
            "content": content,
            "memory_key": memory_key or None,
            "memory_value": candidate.get("memory_value", ""),
            "scope": candidate.get("scope", "general"),
            "source": source,
            "confidence": confidence,
            "status": status,
            "evidence_count": 1,
            "last_seen_at": now,
        }
        result = supabase.table("memory_items").insert(row).execute()
        if not result.data:
            return None
        memory_id = result.data[0]["id"]

    # Always create evidence row
    supabase.table("memory_evidence").insert({
        "memory_id": memory_id,
        "source_type": "message",
        "source_id": source_id,
        "extraction_type": "explicit_statement" if source == "explicit" else "repeated_signal",
        "span": content[:200],
        "extractor_version": "v1",
    }).execute()

    return {"id": memory_id, "content": content, "status": status, "source": source}


# ── Feedback capture ──────────────────────────────────────────────────────────

def record_feedback(
    user_id: str,
    feedback_type: str,
    source_type: str,
    source_id: str,
    memory_id: Optional[str] = None,
    correction: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """
    Record user feedback on an agent output.
    'accepted' boosts confidence. 'edited'/'rejected' decays it.
    'corrected' creates a new memory contradicting the old one.

    Args:
        feedback_type: "accepted" | "edited" | "rejected" | "corrected"
        source_type:   "email_draft" | "chat_response" | "suggested_reply"
        source_id:     ID of the output (email_id or conversation_id)
        memory_id:     If known, which memory this feedback targets
        correction:    The user's corrected version (for "corrected" type)
    """
    try:
        # Save feedback row
        supabase.table("memory_feedback").insert({
            "user_id": user_id,
            "memory_id": memory_id,
            "feedback_type": feedback_type,
            "correction": correction,
            "source_type": source_type,
            "source_id": source_id,
        }).execute()

        # Adjust confidence on the linked memory
        if memory_id:
            result = supabase.table("memory_items").select("confidence").eq("id", memory_id).single().execute()
            if result.data:
                current_conf = result.data["confidence"]
                if feedback_type == "accepted":
                    new_conf = min(1.0, current_conf + 0.05)
                elif feedback_type in ("edited", "rejected"):
                    new_conf = max(MIN_CONFIDENCE, current_conf - 0.1)
                elif feedback_type == "corrected":
                    new_conf = max(MIN_CONFIDENCE, current_conf - 0.2)
                else:
                    new_conf = current_conf

                supabase.table("memory_items").update({
                    "confidence": new_conf,
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", memory_id).execute()

    except Exception as exc:
        logger.warning("Failed to record feedback: %s", exc)


def record_behavior_memory(
    user_id: str,
    tenant_id: str,
    *,
    memory_key: str,
    memory_value: str,
    content: str,
    scope: str = "priority",
    category: str = "preference",
    source_id: str = "",
    confidence: float = 0.6,
) -> Optional[dict]:
    """
    Store a small, evidence-backed behavior signal from an inbox action.

    These are intentionally low-confidence at first. Repeated ignores, later
    actions, drafts, edits, and sends promote them into active preferences.
    """
    candidate = {
        "category": category,
        "content": content,
        "memory_key": memory_key,
        "memory_value": memory_value,
        "scope": scope,
        "source": "implicit",
        "confidence": confidence,
    }
    try:
        return _save_candidate_memory(
            user_id=user_id,
            tenant_id=tenant_id,
            candidate=candidate,
            source_id=source_id,
        )
    except Exception as exc:
        logger.warning("Failed to record behavior memory %s: %s", memory_key, exc)
        return None


def record_correction(
    user_id: str,
    tenant_id: str,
    old_memory_id: str,
    correction_text: str,
    source_id: str = "",
) -> Optional[str]:
    """
    User explicitly corrects a memory. Marks old one as 'contradicted'
    and creates a new 'active' memory with the correction.
    """
    try:
        # Get old memory for reference
        old = supabase.table("memory_items").select("*").eq("id", old_memory_id).single().execute().data
        if not old:
            return None

        # Create new memory from correction
        now = datetime.now(timezone.utc).isoformat()
        new_result = supabase.table("memory_items").insert({
            "user_id": user_id,
            "tenant_id": tenant_id,
            "category": old["category"],
            "content": correction_text,
            "memory_key": old.get("memory_key"),
            "scope": old.get("scope", "general"),
            "source": "explicit",
            "confidence": 0.95,  # high confidence — user explicitly stated this
            "status": "active",
            "evidence_count": 1,
            "last_seen_at": now,
        }).execute()

        new_id = new_result.data[0]["id"]

        # Mark old memory as contradicted
        supabase.table("memory_items").update({
            "status": "contradicted",
            "contradicted_by": new_id,
        }).eq("id", old_memory_id).execute()

        # Create evidence for the correction
        supabase.table("memory_evidence").insert({
            "memory_id": new_id,
            "source_type": "explicit_command",
            "source_id": source_id,
            "extraction_type": "correction",
            "span": correction_text[:200],
        }).execute()

        logger.info("Memory corrected: %s → %s", old_memory_id, new_id)
        return new_id

    except Exception as exc:
        logger.error("Failed to record correction: %s", exc)
        return None


# ── Maintenance ───────────────────────────────────────────────────────────────

def decay_stale_memories() -> int:
    """
    Reduce confidence on memories not seen in DECAY_AFTER_DAYS days.
    Delete memories that fall below MIN_CONFIDENCE.
    Called by a Celery beat task (daily).

    Returns:
        Number of memories updated.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DECAY_AFTER_DAYS)).isoformat()
    count = 0

    try:
        stale_result = (
            supabase.table("memory_items")
            .select("id, confidence")
            .eq("status", "active")
            .lt("last_seen_at", cutoff)
            .execute()
        )
        stale = stale_result.data or []

        for item in stale:
            new_conf = round(item["confidence"] - 0.1, 2)
            if new_conf < MIN_CONFIDENCE:
                supabase.table("memory_items").update({"status": "expired"}).eq("id", item["id"]).execute()
            else:
                supabase.table("memory_items").update({"confidence": new_conf}).eq("id", item["id"]).execute()
            count += 1

        if count:
            logger.info("Decayed %d stale memories", count)

    except Exception as exc:
        logger.warning("Memory decay failed: %s", exc)

    return count
