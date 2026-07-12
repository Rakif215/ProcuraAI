"""
app/api/v1/emails.py
---------------------
REST API endpoints for:
  - Email account management (connect, list, delete, sync)
  - Email inbox (list, filter, detail, mark responded)
  - Send reply

All endpoints are tenant-scoped — users only see their own data.
"""
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status, BackgroundTasks
from pydantic import BaseModel

from app.core.deps import AuthUser
from app.core.crypto import encrypt_password
from app.db.client import supabase
from app.services.email_service import send_email
from app.services.assistant_intents import classify_email_intent
from app.services.telemetry import log_event
from app.services.memory_service import record_feedback, record_behavior_memory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/emails", tags=["emails"])

PROVIDER_PRESETS = {
    "spacemail": {
        "imap_host": "mail.spacemail.com",
        "imap_port": 993,
        "smtp_host": "mail.spacemail.com",
        "smtp_port": 465,
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    "outlook": {
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
    },
    "fau": {
        "imap_host": "groupware.fau.de",
        "imap_port": 993,
        "smtp_host": "groupware.fau.de",
        "smtp_port": 587,
    },
}


IMPORTANT_CATEGORIES = {"po", "rfq", "quotation", "invoice", "finance", "logistics"}
LOW_PRIORITY_CATEGORIES = {"bounce", "internal", "other", "promotion", "marketing"}

ROLE_CATEGORY_WEIGHTS = {
    "founder": {"rfq": 18, "quotation": 16, "po": 14, "finance": 10, "invoice": 8, "logistics": 8},
    "sales": {"rfq": 22, "quotation": 20, "po": 12},
    "manager": {"po": 14, "logistics": 12, "finance": 10, "invoice": 8},
    "logistics": {"logistics": 24, "po": 16},
    "finance": {"finance": 24, "invoice": 22, "po": 8},
}


def _qatar_day_bounds(days_back: int = 0) -> tuple[str, str]:
    qatar = ZoneInfo("Asia/Qatar")
    day = datetime.now(qatar).date() - timedelta(days=days_back)
    start = datetime.combine(day, time.min, tzinfo=qatar).astimezone(timezone.utc)
    end = datetime.combine(day, time.max, tzinfo=qatar).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def _sender_name(email: dict) -> str:
    return (email.get("sender") or "Unknown").split("<")[0].strip() or "Unknown"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")[:80] or "unknown"


def _load_adaptation_signals(user: AuthUser) -> dict:
    signals = {
        "role": "",
        "tone": "professional",
        "email_length": "medium",
        "category_actions": {},
        "sender_actions": {},
    }
    try:
        profile = (
            supabase.table("user_profiles")
            .select("role, tone, email_length")
            .eq("user_id", user.user_id)
            .single()
            .execute()
        ).data or {}
        signals.update({k: profile.get(k) or signals[k] for k in ("role", "tone", "email_length")})
    except Exception:
        pass

    try:
        memories = (
            supabase.table("memory_items")
            .select("memory_key, memory_value, confidence, status")
            .eq("user_id", user.user_id)
            .in_("status", ["active", "candidate"])
            .gte("confidence", 0.55)
            .limit(50)
            .execute()
        ).data or []
        for memory in memories:
            key = memory.get("memory_key") or ""
            value = memory.get("memory_value") or ""
            if key.startswith("category_action_"):
                signals["category_actions"][key.replace("category_action_", "", 1)] = value
            if key.startswith("sender_action_"):
                signals["sender_actions"][key.replace("sender_action_", "", 1)] = value
    except Exception:
        pass
    return signals


def _score_email(email: dict, signals: Optional[dict] = None) -> tuple[int, str]:
    score = 0
    reasons = []
    category = (email.get("category") or "").lower()
    priority = (email.get("priority") or "normal").lower()
    subject = (email.get("subject") or "").lower()
    sender = (email.get("sender") or "").lower()
    sender_key = _slug(_sender_name(email))
    signals = signals or {}

    if priority == "urgent":
        score += 100
        reasons.append("urgent")
    elif priority == "normal":
        score += 20
    else:
        score -= 80

    if email.get("needs_response") and not email.get("responded"):
        score += 55
        reasons.append("needs reply")

    if category in IMPORTANT_CATEGORIES:
        score += 25
        reasons.append(category)
    if category in LOW_PRIORITY_CATEGORIES:
        score -= 35

    role = (signals.get("role") or "").lower()
    role_boost = ROLE_CATEGORY_WEIGHTS.get(role, {}).get(category, 0)
    if role_boost:
        score += role_boost
        reasons.append(f"{role} priority")

    category_action = (signals.get("category_actions") or {}).get(category)
    sender_action = (signals.get("sender_actions") or {}).get(sender_key)
    for action in {category_action, sender_action}:
        if action in {"draft", "sent"}:
            score += 18
            reasons.append("matches your past actions")
        elif action == "later":
            score += 4
            reasons.append("usually saved for later")
        elif action == "ignore":
            score -= 35
            reasons.append("usually ignored")

    important_terms = ["invoice", "payment", "rfq", "quote", "quotation", "po", "delivery", "shipment", "customs", "approval", "contract", "deadline", "overdue"]
    if any(term in subject for term in important_terms):
        score += 20
        reasons.append("business keyword")

    noisy_terms = ["newsletter", "promotion", "offer", "discount", "alert", "jobs", "tripadvisor", "glassdoor", "spotify", "canva"]
    if any(term in subject or term in sender for term in noisy_terms):
        score -= 30

    try:
        received = datetime.fromisoformat((email.get("received_at") or "").replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours > 24 and email.get("needs_response") and not email.get("responded"):
            score += 30
            reasons.append("unanswered")
    except Exception:
        pass

    return score, ", ".join(dict.fromkeys(reasons)) or "worth a quick review"


def _brief_email(email: dict, queue_item: Optional[dict] = None, signals: Optional[dict] = None) -> dict:
    intent = classify_email_intent(email, signals)
    item = {
        "id": email["id"],
        "sender": _sender_name(email),
        "subject": email.get("subject") or "(no subject)",
        "summary": email.get("summary") or "",
        "priority": email.get("priority") or "normal",
        "category": email.get("category"),
        "needs_response": bool(email.get("needs_response")),
        "responded": bool(email.get("responded")),
        "received_at": email.get("received_at"),
        "suggested_next_action": intent,
        "intent": intent,
    }
    if queue_item:
        item.update({
            "queue_id": queue_item.get("id"),
            "queue_status": queue_item.get("status"),
            "reason": queue_item.get("reason"),
            "priority_score": queue_item.get("priority_score"),
        })
    return item


def _remember_queue_action(user: AuthUser, queue_item: dict, email: dict, action: str) -> None:
    category = (email.get("category") or "other").lower()
    sender = _sender_name(email)
    sender_key = _slug(sender)
    action_label = {
        "ignore": "ignored",
        "later": "saved for later",
        "draft": "drafted",
        "sent": "sent",
    }.get(action, action)

    record_behavior_memory(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        memory_key=f"category_action_{_slug(category)}",
        memory_value=action,
        content=f"User usually {action_label} {category} emails.",
        scope="priority",
        source_id=email.get("id", ""),
        confidence=0.6,
    )
    record_behavior_memory(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        memory_key=f"sender_action_{sender_key}",
        memory_value=action,
        content=f"User {action_label} emails from {sender}.",
        scope="priority",
        source_id=email.get("id", ""),
        confidence=0.6,
    )


def _upsert_queue_item(user: AuthUser, email: dict, score: int, reason: str) -> Optional[dict]:
    if score < 45:
        return None
    try:
        existing = (
            supabase.table("assistant_queue_items")
            .select("*")
            .eq("tenant_id", user.tenant_id)
            .eq("email_id", email["id"])
            .limit(1)
            .execute()
        ).data
        if existing:
            item = existing[0]
            if item.get("status") in {"ignored", "sent"}:
                return item
            result = (
                supabase.table("assistant_queue_items")
                .update({
                    "reason": reason,
                    "priority_score": score,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("id", item["id"])
                .execute()
            )
            return (result.data or [item])[0]

        result = supabase.table("assistant_queue_items").insert({
            "tenant_id": user.tenant_id,
            "user_id": user.user_id,
            "email_id": email["id"],
            "status": "pending",
            "reason": reason,
            "priority_score": score,
        }).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.warning("Could not upsert queue item for email %s: %s", email.get("id"), exc)
        return None


def _build_reply_draft(email: dict, signals: Optional[dict] = None) -> str:
    existing = (email.get("suggested_reply") or "").strip()
    if existing:
        return existing
    sender = _sender_name(email)
    subject = email.get("subject") or "your email"
    signals = signals or {}
    length = (signals.get("email_length") or "medium").lower()
    category = (email.get("category") or "").lower()
    subject_lower = subject.lower()
    summary = (email.get("summary") or "").lower()
    intent = classify_email_intent(email, signals)
    domain_intent = intent.get("domain_intent")
    commercial = intent.get("commercial") or {}

    if domain_intent in {"suggest_times", "confirm_deployment_window"}:
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            "Thanks for reaching out. I can do either tomorrow morning or the following afternoon. "
            "Please let me know which time works better for you.\n\n"
            "Best regards,"
        )
    if domain_intent in {"ask_eta", "confirm_po_status", "confirm_delivery_date"}:
        po = f" for PO {commercial.get('po_number')}" if commercial.get("po_number") else ""
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            f"Thanks for the update{po}. Could you please share the latest ETA, tracking details, and delivery status?\n\n"
            "Best regards,"
        )
    if domain_intent in {"request_invoice", "request_packing_list", "request_certificate", "request_tracking", "request_po"}:
        missing = (commercial.get("missing_document") or "document").replace("_", " ")
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            f"Thanks for your message. Could you please send the missing {missing} when available?\n\n"
            "Best regards,"
        )
    if domain_intent in {"counter_offer", "commercial_counter_offer"}:
        amount = f" of {commercial.get('currency')} {commercial.get('amount')}" if commercial.get("amount") and commercial.get("currency") else ""
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            f"Thank you for sharing the proposal{amount}. We have reviewed it and would appreciate your best revised offer for approval.\n\n"
            "Best regards,"
        )
    if domain_intent == "ask_for_logs":
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            "Thanks for flagging this. Could you please share the logs, screenshots, environment details, and steps to reproduce?\n\n"
            "Best regards,"
        )
    if category == "logistics" or any(term in subject_lower for term in ["delivery", "shipment", "customs"]):
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            "Thanks for the update. Please share the expected timing once confirmed.\n\n"
            "Best regards,"
        )
    if length == "short":
        return (
            f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
            "Thanks for your message. I am checking this and will come back to you shortly.\n\n"
            "Best regards,"
        )
    return (
        f"Hi {sender.split()[0] if sender != 'Unknown' else 'there'},\n\n"
        f"Thanks for your message about {subject}. I am checking this and will come back to you shortly.\n\n"
        "Best regards,"
    )


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConnectEmailRequest(BaseModel):
    email: str
    password: str
    username: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = 993
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 465
    provider: str = "imap"


class SendReplyRequest(BaseModel):
    email_id: str            # Orion email UUID to reply to
    body: str                # The reply body (written by user or AI)
    account_id: Optional[str] = None  # If None, uses the tenant's first account


class QueueActionRequest(BaseModel):
    note: Optional[str] = None


class QueueDraftRequest(BaseModel):
    draft: Optional[str] = None


# ── Email Accounts ────────────────────────────────────────────────────────────

@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def connect_email_account(body: ConnectEmailRequest, user: AuthUser):
    """
    Connect a new email account (IMAP/SMTP).
    The password is encrypted before storage.
    """
    from app.core.config import settings

    preset = PROVIDER_PRESETS.get(body.provider, {})
    imap_host = body.imap_host or preset.get("imap_host") or settings.default_imap_host
    imap_port = body.imap_port or preset.get("imap_port") or settings.default_imap_port
    smtp_host = body.smtp_host or preset.get("smtp_host") or settings.default_smtp_host
    smtp_port = body.smtp_port or preset.get("smtp_port") or settings.default_smtp_port

    # Test the IMAP connection before saving
    try:
        import imaplib
        conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        conn.login(body.username or body.email, body.password)
        conn.logout()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"IMAP connection failed: {str(exc)}"
        )

    # Save the account
    try:
        result = supabase.table("email_accounts").upsert({
            "tenant_id": user.tenant_id,
            "email": body.email,
            "username": body.username,
            "password_encrypted": encrypt_password(body.password),
            "provider": body.provider,
            "imap_host": imap_host,
            "imap_port": imap_port,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "is_active": True,
        }, on_conflict="tenant_id,email").execute()

        account = result.data[0]
        logger.info("Connected email account %s for tenant %s", body.email, user.tenant_id)

        # Trigger immediate sync (fire and forget)
        try:
            from app.workers.email_poller import poll_single_account
            poll_single_account.delay(account["id"])
        except Exception:
            pass  # Redis may not be running locally — that's OK

        return {"account_id": account["id"], "email": body.email, "status": "connected"}

    except Exception as exc:
        logger.error("Failed to save email account: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to connect account: {str(exc)}")


@router.get("/accounts")
async def list_email_accounts(user: AuthUser):
    """List all connected email accounts for this tenant."""
    try:
        result = (
            supabase.table("email_accounts")
            .select("id, email, provider, imap_host, is_active, last_synced_at, created_at")
            .eq("tenant_id", user.tenant_id)
            .order("created_at")
            .execute()
        )
        return result.data or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/accounts/{account_id}/status")
async def email_account_status(account_id: str, user: AuthUser):
    """Return lightweight sync health for a connected email account."""
    try:
        result = (
            supabase.table("email_accounts")
            .select("id, email, provider, is_active, last_synced_at, created_at")
            .eq("id", account_id)
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Account not found")
        return {
            **result.data,
            "status": "connected" if result.data.get("is_active") else "inactive",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_account(account_id: str, user: AuthUser):
    """Disconnect and delete an email account."""
    try:
        supabase.table("email_accounts").delete().eq("id", account_id).eq(
            "tenant_id", user.tenant_id
        ).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/accounts/{account_id}/sync")
async def sync_email_account(
    account_id: str,
    user: AuthUser,
    background_tasks: BackgroundTasks,
    scan_window_days: Optional[int] = None,
):
    """
    Manually trigger an email sync for a specific account.
    Runs in the background via Celery (or inline if Celery not available).
    """
    # Verify ownership
    result = (
        supabase.table("email_accounts")
        .select("id, email")
        .eq("id", account_id)
        .eq("tenant_id", user.tenant_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        from app.workers.email_poller import poll_single_account
        task = poll_single_account.delay(account_id, scan_window_days=scan_window_days)
        return {"status": "syncing", "task_id": task.id, "email": result.data["email"]}
    except Exception:
        # Celery not running — do sync inline (slower but works without Redis)
        from app.services.email_service import fetch_and_triage
        full_account = (
            supabase.table("email_accounts")
            .select("*")
            .eq("id", account_id)
            .single()
            .execute()
        ).data
        background_tasks.add_task(fetch_and_triage, full_account, scan_window_days=scan_window_days)
        log_event("email_sync_started", tenant_id=user.tenant_id, user_id=user.user_id, properties={"account_id": account_id, "mode": "background"})
        return {"status": "syncing", "email": result.data["email"]}


# ── Emails ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_emails(
    user: AuthUser,
    filter: Optional[str] = None,   # "urgent" | "unread" | "responded"
    responded: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    List emails for this tenant with optional filtering and search.
    filter: "urgent" | "unread" | "responded"
    """
    try:
        query = (
            supabase.table("emails")
            .select("id, sender, subject, summary, priority, category, sentiment, needs_response, responded, received_at, created_at")
            .eq("tenant_id", user.tenant_id)
        )

        if filter == "urgent":
            query = query.eq("priority", "urgent")
        elif filter == "unread":
            query = query.eq("responded", False).eq("needs_response", True)
        elif filter == "responded":
            query = query.eq("responded", True)

        if responded is not None:
            query = query.eq("responded", responded)
            if responded is False:
                query = query.eq("needs_response", True)

        if search:
            query = query.or_(
                f"subject.ilike.%{search}%,"
                f"sender.ilike.%{search}%,"
                f"summary.ilike.%{search}%"
            )

        result = (
            query
            .order("received_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return result.data or []

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/stats")
async def get_email_stats(user: AuthUser):
    """Return inbox statistics for the dashboard."""
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()

        all_emails = (
            supabase.table("emails")
            .select("priority, responded, received_at, needs_response")
            .eq("tenant_id", user.tenant_id)
            .execute()
        ).data or []

        total = len(all_emails)
        urgent = sum(1 for e in all_emails if e.get("priority") == "urgent")
        unread = sum(1 for e in all_emails if not e.get("responded"))
        needs_response = sum(1 for e in all_emails if e.get("needs_response") and not e.get("responded"))
        today_count = sum(1 for e in all_emails if e.get("received_at", "").startswith(today))

        return {
            "total": total,
            "urgent": urgent,
            "unread": unread,
            "needs_response": needs_response,
            "today": today_count,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/digest/today")
async def get_today_digest(user: AuthUser):
    """
    Return an assistant-style digest for today's inbox without running the LLM.
    This is the controlled, low-token summary path for Orion Lite.
    """
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        result = (
            supabase.table("emails")
            .select("id, sender, subject, summary, priority, category, needs_response, responded, received_at")
            .eq("tenant_id", user.tenant_id)
            .gte("received_at", today)
            .order("received_at", desc=True)
            .limit(50)
            .execute()
        )
        rows = result.data or []
        urgent = [e for e in rows if e.get("priority") == "urgent"]
        needs_reply = [e for e in rows if e.get("needs_response") and not e.get("responded")]
        ignored = [
            e for e in rows
            if e.get("priority") == "low" or e.get("category") in {"bounce", "internal", "other"}
        ]

        def brief(email: dict) -> dict:
            sender = (email.get("sender") or "Unknown").split("<")[0].strip()
            return {
                "id": email["id"],
                "sender": sender,
                "subject": email.get("subject"),
                "summary": email.get("summary"),
                "priority": email.get("priority"),
                "category": email.get("category"),
                "needs_response": email.get("needs_response"),
                "received_at": email.get("received_at"),
            }

        return {
            "date": today,
            "total": len(rows),
            "urgent_count": len(urgent),
            "needs_reply_count": len(needs_reply),
            "ignored_count": len(ignored),
            "urgent": [brief(e) for e in urgent[:5]],
            "needs_reply": [brief(e) for e in needs_reply[:8]],
            "ignored": [brief(e) for e in ignored[:5]],
            "message": (
                f"You have {len(rows)} emails today. "
                f"{len(urgent)} urgent, {len(needs_reply)} need replies, "
                f"and {len(ignored)} look low-priority."
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/assistant/brief")
async def get_assistant_brief(user: AuthUser, refresh: bool = False):
    """
    Calm executive-assistant brief for Orion Lite.
    Surfaces only the top 1-3 important items and batches low-priority mail.
    """
    try:
        if refresh:
            try:
                from app.services.email_service import fetch_and_triage
                accounts = (
                    supabase.table("email_accounts")
                    .select("*")
                    .eq("tenant_id", user.tenant_id)
                    .eq("is_active", True)
                    .execute()
                ).data or []
                for account in accounts:
                    fetch_and_triage(account)
            except Exception as exc:
                logger.warning("Assistant brief refresh sync skipped: %s", exc)

        today_start, today_end = _qatar_day_bounds(0)
        week_start = (datetime.now(ZoneInfo("Asia/Qatar")) - timedelta(days=7)).astimezone(timezone.utc).isoformat()
        signals = _load_adaptation_signals(user)

        today_rows = (
            supabase.table("emails")
            .select("id, sender, subject, summary, body_text, priority, category, needs_response, responded, received_at, suggested_reply")
            .eq("tenant_id", user.tenant_id)
            .gte("received_at", today_start)
            .lte("received_at", today_end)
            .order("received_at", desc=True)
            .limit(75)
            .execute()
        ).data or []

        overdue_rows = (
            supabase.table("emails")
            .select("id, sender, subject, summary, body_text, priority, category, needs_response, responded, received_at, suggested_reply")
            .eq("tenant_id", user.tenant_id)
            .eq("responded", False)
            .eq("needs_response", True)
            .gte("received_at", week_start)
            .lt("received_at", today_start)
            .order("received_at", desc=True)
            .limit(25)
            .execute()
        ).data or []

        candidates_by_id = {e["id"]: e for e in overdue_rows + today_rows}
        scored = []
        low_priority_count = 0
        for email in candidates_by_id.values():
            score, reason = _score_email(email, signals)
            if score < 25:
                low_priority_count += 1
            if score >= 45 and not email.get("responded"):
                item = _upsert_queue_item(user, email, score, reason)
                scored.append((score, email, item))

        scored.sort(key=lambda entry: entry[0], reverse=True)
        top = [_brief_email(email, item, signals) for _, email, item in scored[:3]]

        pending_count = (
            supabase.table("assistant_queue_items")
            .select("id", count="exact")
            .eq("tenant_id", user.tenant_id)
            .eq("status", "pending")
            .limit(1)
            .execute()
        ).count or 0

        if top:
            message = f"Good morning. I found {len(top)} thing{'s' if len(top) != 1 else ''} worth your attention."
        elif today_rows:
            message = f"Good morning. Nothing needs action right now. I batched {len(today_rows)} low-priority emails."
        else:
            message = "Good morning. Your inbox is quiet right now."

        log_event(
            "assistant_brief_viewed",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            properties={
                "today_total": len(today_rows),
                "overdue_unanswered": len(overdue_rows),
                "low_priority_batched": low_priority_count,
                "queue_items_created": len(top),
                "refresh": refresh,
            },
        )

        return {
            "message": message,
            "top_items": top,
            "pending_count": pending_count,
            "digest": {
                "today_total": len(today_rows),
                "overdue_unanswered": len(overdue_rows),
                "low_priority_batched": low_priority_count,
            },
            "tone": "calm",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/assistant/queue")
async def get_assistant_queue(user: AuthUser, status_filter: str = "pending", limit: int = 10):
    try:
        queue_items = (
            supabase.table("assistant_queue_items")
            .select("*")
            .eq("tenant_id", user.tenant_id)
            .eq("status", status_filter)
            .order("priority_score", desc=True)
            .order("created_at", desc=False)
            .limit(max(1, min(limit, 25)))
            .execute()
        ).data or []

        if not queue_items:
            return {"items": []}

        email_ids = [item["email_id"] for item in queue_items]
        email_rows = (
            supabase.table("emails")
            .select("id, sender, subject, summary, body_text, priority, category, needs_response, responded, received_at, suggested_reply")
            .eq("tenant_id", user.tenant_id)
            .in_("id", email_ids)
            .execute()
        ).data or []
        emails_by_id = {email["id"]: email for email in email_rows}
        signals = _load_adaptation_signals(user)
        items = [
            _brief_email(emails_by_id[item["email_id"]], item, signals)
            for item in queue_items
            if item["email_id"] in emails_by_id
        ]
        return {"items": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/assistant/queue/{queue_id}/ignore")
async def ignore_queue_item(queue_id: str, body: QueueActionRequest, user: AuthUser):
    try:
        result = supabase.table("assistant_queue_items").update({
            "status": "ignored",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", queue_id).eq("tenant_id", user.tenant_id).execute()
        log_event("assistant_queue_ignored", tenant_id=user.tenant_id, user_id=user.user_id, properties={"queue_id": queue_id})
        queue_item = (result.data or [None])[0]
        if queue_item:
            email_row = (
                supabase.table("emails")
                .select("id, sender, subject, category")
                .eq("id", queue_item.get("email_id"))
                .eq("tenant_id", user.tenant_id)
                .single()
                .execute()
            ).data
            if email_row:
                _remember_queue_action(user, queue_item, email_row, "ignore")
            record_feedback(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                feedback_type="rejected",
                source_type="email_draft",
                source_id=queue_item.get("email_id", ""),
            )
        return {"status": "ignored", "item": (result.data or [None])[0]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/assistant/queue/{queue_id}/later")
async def save_queue_item_for_later(queue_id: str, body: QueueActionRequest, user: AuthUser):
    try:
        result = supabase.table("assistant_queue_items").update({
            "status": "later",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", queue_id).eq("tenant_id", user.tenant_id).execute()
        log_event("assistant_queue_later", tenant_id=user.tenant_id, user_id=user.user_id, properties={"queue_id": queue_id})
        queue_item = (result.data or [None])[0]
        if queue_item:
            email_row = (
                supabase.table("emails")
                .select("id, sender, subject, category")
                .eq("id", queue_item.get("email_id"))
                .eq("tenant_id", user.tenant_id)
                .single()
                .execute()
            ).data
            if email_row:
                _remember_queue_action(user, queue_item, email_row, "later")
            record_feedback(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                feedback_type="edited",
                source_type="email_draft",
                source_id=queue_item.get("email_id", ""),
                correction="Saved for later",
            )
        return {"status": "later", "item": (result.data or [None])[0]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/assistant/queue/{queue_id}/draft")
async def draft_queue_reply(queue_id: str, body: QueueDraftRequest, user: AuthUser):
    try:
        item = (
            supabase.table("assistant_queue_items")
            .select("*")
            .eq("id", queue_id)
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        ).data
        email_row = (
            supabase.table("emails")
            .select("*")
            .eq("id", item["email_id"])
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        ).data
        draft = (body.draft or "").strip() or _build_reply_draft(email_row, _load_adaptation_signals(user))
        supabase.table("emails").update({"suggested_reply": draft}).eq("id", email_row["id"]).execute()
        updated = supabase.table("assistant_queue_items").update({
            "status": "drafted",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", queue_id).eq("tenant_id", user.tenant_id).execute()
        log_event("assistant_draft_generated", tenant_id=user.tenant_id, user_id=user.user_id, properties={"queue_id": queue_id, "email_id": email_row["id"]})
        _remember_queue_action(user, item, email_row, "draft")
        return {"status": "drafted", "draft": draft, "email_id": email_row["id"], "item": (updated.data or [None])[0]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{email_id}")
async def get_email(email_id: str, user: AuthUser):
    """Get full details for a single email."""
    try:
        result = (
            supabase.table("emails")
            .select("*")
            .eq("id", email_id)
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Email not found")
        return result.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{email_id}/mark-responded")
async def mark_email_responded(email_id: str, user: AuthUser):
    """Mark an email as responded to."""
    from datetime import datetime, timezone

    try:
        supabase.table("emails").update({
            "responded": True,
            "responded_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", email_id).eq("tenant_id", user.tenant_id).execute()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/send-reply")
async def send_reply(body: SendReplyRequest, user: AuthUser):
    """
    Send an email reply via SMTP.
    Marks the original email as responded after successful send.
    """
    # Get the original email
    try:
        email_result = (
            supabase.table("emails")
            .select("id, sender, subject, account_id, category, suggested_reply")
            .eq("id", body.email_id)
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        )
        original = email_result.data
        if not original:
            raise HTTPException(status_code=404, detail="Email not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Get the email account to send from
    account_id = body.account_id or original.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="No email account configured for sending")

    try:
        account_result = (
            supabase.table("email_accounts")
            .select("*")
            .eq("id", account_id)
            .eq("tenant_id", user.tenant_id)
            .single()
            .execute()
        )
        account = account_result.data
        if not account:
            raise HTTPException(status_code=404, detail="Email account not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Send
    success = send_email(
        account=account,
        to_address=original["sender"],
        subject=original["subject"],
        body=body.body,
    )

    if not success:
        raise HTTPException(status_code=502, detail="Failed to send email — check SMTP configuration")

    # Mark as responded
    from datetime import datetime, timezone
    supabase.table("emails").update({
        "responded": True,
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.email_id).execute()

    supabase.table("assistant_queue_items").update({
        "status": "sent",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("email_id", body.email_id).eq("tenant_id", user.tenant_id).execute()

    _remember_queue_action(user, {"email_id": body.email_id}, original, "sent")
    suggested = (original.get("suggested_reply") or "").strip()
    sent_body = (body.body or "").strip()
    if suggested and sent_body:
        suggested_words = max(1, len(suggested.split()))
        sent_words = len(sent_body.split())
        if sent_words <= suggested_words * 0.7:
            record_behavior_memory(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                memory_key="draft_length_adjustment",
                memory_value="shorter",
                content="User often shortens AI email drafts; keep future replies concise.",
                scope="email_drafting",
                source_id=body.email_id,
                confidence=0.62,
            )
        elif sent_words >= suggested_words * 1.35:
            record_behavior_memory(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                memory_key="draft_length_adjustment",
                memory_value="more_detail",
                content="User often expands AI email drafts; include more context in future replies.",
                scope="email_drafting",
                source_id=body.email_id,
                confidence=0.62,
            )

    log_event("assistant_reply_sent", tenant_id=user.tenant_id, user_id=user.user_id, properties={"email_id": body.email_id})

    logger.info("Reply sent for email %s by user %s", body.email_id, user.user_id)
    return {"status": "sent"}
