"""
app/services/email_service.py
------------------------------
Core email service for Phase 3 — real IMAP fetching and SMTP sending.

Ported and upgraded from MailBrain's core/fetcher.py and core/mailer.py
to work with:
  - Multi-tenant (per-account credentials from the DB)
  - Supabase email_accounts + emails tables
  - AI triage via Groq (same provider as the agent LLM — no extra dep)

Architecture:
  fetch_and_triage(account)  → connects IMAP, fetches recent inbox mail, AI-triages selected items, saves to DB
  send_email(account, ...)   → sends via SMTP SSL, marks email as responded in DB
"""
import email as _email_lib
import imaplib
import json
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.crypto import decrypt_password, encrypt_password
from app.db.client import supabase
from app.services.model_router import chat_completion
from app.services.telemetry import log_event

logger = logging.getLogger(__name__)

DEFAULT_SYNC_POLICY = {
    "scan_window_days": 1,
    "max_emails_per_sync": 25,
    "ai_triage_limit": 5,
    "auto_ai_triage": True,
}

# ── Categories and rules (ported from MailBrain processor) ───────────────────

_VALID_PRIORITIES = {"urgent", "normal", "low"}
_VALID_SENTIMENTS = {"positive", "neutral", "negative", "angry"}

_URGENT_PATTERNS = re.compile(
    r"\bOVERDUE\b|URGENT REMINDER|\bpast due\b|within 24 hours"
    r"|BCD\s*:\s*\d|cleared customs",
    re.IGNORECASE,
)

_URGENT_CATEGORIES  = {"po"}
_NORMAL_CATEGORIES  = {"rfq", "quotation", "finance"}
_LOW_CATEGORIES     = {"bounce", "internal", "other"}
_RESPOND_TRUE       = {"rfq", "po", "invoice", "quotation", "finance"}
_RESPOND_FALSE      = {"bounce", "internal", "other"}


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _get_plain_credential(account: dict) -> str:
    """Return decrypted password or OAuth marker value."""
    return decrypt_password(account["password_encrypted"])


def _refresh_google_access_token(account: dict) -> Optional[str]:
    """Refresh Gmail OAuth access token and persist it back to email_accounts."""
    refresh_token_encrypted = account.get("oauth_refresh_token")
    if not refresh_token_encrypted:
        logger.warning("Gmail account %s has no refresh token", account.get("email"))
        return None

    refresh_token = decrypt_password(refresh_token_encrypted)
    try:
        response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        access_token = response.json()["access_token"]

        account["password_encrypted"] = encrypt_password(f"oauth2:{access_token}")
        if account.get("id"):
            supabase.table("email_accounts").update({
                "password_encrypted": account["password_encrypted"],
            }).eq("id", account["id"]).execute()
        return access_token
    except Exception as exc:
        logger.error("Failed to refresh Gmail access token for %s: %s", account.get("email"), exc)
        return None


def _get_oauth_access_token(account: dict) -> Optional[str]:
    credential = _get_plain_credential(account)
    if credential.startswith("oauth2:"):
        return credential.removeprefix("oauth2:")
    return None


def _xoauth2_string(email_addr: str, access_token: str) -> str:
    return f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"


def _coerce_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _get_sync_policy(tenant_id: str) -> dict:
    """Load per-tenant pilot sync controls from user_profiles.preferences."""
    policy = dict(DEFAULT_SYNC_POLICY)
    try:
        result = (
            supabase.table("user_profiles")
            .select("preferences")
            .eq("tenant_id", tenant_id)
            .limit(1)
            .execute()
        )
        preferences = (result.data or [{}])[0].get("preferences") or {}
        sync_policy = preferences.get("sync_policy") or {}
        policy["scan_window_days"] = _coerce_int(sync_policy.get("scan_window_days"), policy["scan_window_days"], 1, 30)
        policy["max_emails_per_sync"] = _coerce_int(sync_policy.get("max_emails_per_sync"), policy["max_emails_per_sync"], 1, 200)
        hard_limit = max(0, settings.ai_triage_hard_limit_per_sync)
        policy["ai_triage_limit"] = _coerce_int(sync_policy.get("ai_triage_limit"), policy["ai_triage_limit"], 0, hard_limit)
        if "auto_ai_triage" in sync_policy:
            policy["auto_ai_triage"] = bool(sync_policy.get("auto_ai_triage"))
    except Exception as exc:
        logger.warning("Using default sync policy for tenant %s: %s", tenant_id, exc)
    return policy


def _should_ai_triage(parsed: dict, rule_analysis: dict) -> bool:
    """Spend AI only on emails likely to matter."""
    combined = f"{parsed.get('sender', '')} {parsed.get('subject', '')} {parsed.get('body_text', '')}".lower()
    if rule_analysis.get("priority") == "urgent":
        return True
    if rule_analysis.get("needs_response"):
        return True
    keywords = [
        "rfq", "quotation", "quote", "purchase order", " po ", "invoice",
        "payment", "shipment", "delivery", "customs", "urgent", "asap",
        "confirm", "approval", "overdue", "reminder",
    ]
    return any(keyword in combined for keyword in keywords)

def _decode_header_value(value: Optional[str]) -> str:
    """Safely decode RFC 2047-encoded email header."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_body(msg: _email_lib.message.Message) -> str:
    """Walk the MIME tree and return the best plain-text body."""
    plain_parts, html_parts = [], []

    def _decode_part(part):
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        return payload.decode(charset, errors="replace") if payload else ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            if ct == "text/plain":
                plain_parts.append(_decode_part(part))
            elif ct == "text/html":
                html_parts.append(_decode_part(part))
    else:
        decoded = _decode_part(msg)
        if msg.get_content_type() == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        soup = BeautifulSoup("\n".join(html_parts), "html.parser")
        return soup.get_text(separator="\n").strip()
    return ""


def _parse_raw_email(uid: bytes, raw_data: bytes, account_email: str) -> dict:
    """Parse a raw RFC 822 email into a clean dict."""
    msg = _email_lib.message_from_bytes(raw_data)

    raw_from = msg.get("From", "")
    _, sender_addr = parseaddr(raw_from)
    sender = sender_addr if sender_addr else _decode_header_value(raw_from)

    subject = _decode_header_value(msg.get("Subject")) or "(No Subject)"

    date_str = msg.get("Date", "")
    try:
        received_at = parsedate_to_datetime(date_str).isoformat()
    except Exception:
        received_at = datetime.now(timezone.utc).isoformat()

    body_text = _extract_body(msg)
    raw_message_id = (msg.get("Message-ID") or "").strip()
    uid_str = uid.decode("ascii") if isinstance(uid, bytes) else str(uid)

    return {
        "imap_uid": f"{account_email}:{uid_str}",
        "sender": sender,
        "subject": subject,
        "received_at": received_at,
        "body_text": body_text[:8000],  # cap at 8K chars
        "raw_message_id": raw_message_id,
    }


def _connect_imap(account: dict):
    """Connect and authenticate to IMAP for the given account dict."""
    host = account.get("imap_host", settings.default_imap_host)
    port = account.get("imap_port", settings.default_imap_port)
    email_addr = account["email"]
    username = account.get("username") or email_addr
    password = _get_plain_credential(account)

    logger.info("Connecting IMAP %s:%s for %s", host, port, email_addr)
    conn = imaplib.IMAP4_SSL(host, port)
    if password.startswith("oauth2:"):
        access_token = password.removeprefix("oauth2:")
        try:
            conn.authenticate("XOAUTH2", lambda _: _xoauth2_string(email_addr, access_token).encode())
        except imaplib.IMAP4.error:
            access_token = _refresh_google_access_token(account)
            if not access_token:
                raise
            conn.authenticate("XOAUTH2", lambda _: _xoauth2_string(email_addr, access_token).encode())
    else:
        conn.login(username, password)
    return conn


# ── AI Triage ─────────────────────────────────────────────────────────────────

_TRIAGE_SYSTEM_PROMPT = """\
You are an email intelligence assistant for a business. Analyse the email and return \
ONLY a valid JSON object — no markdown, no explanation.

Return exactly these fields:
- summary: 1-2 sentence summary of the email (string)
- priority: one of "urgent", "normal", "low"
- needs_response: true or false
- category: one of "rfq", "po", "logistics", "invoice", "quotation", "finance", "internal", "bounce", "other"
- sentiment: one of "positive", "neutral", "negative", "angry"
- action_items: list of specific actions required (list of strings, can be empty [])
- suggested_reply: a professional draft reply (3-5 sentences), or "" if no response needed

Priority rules:
- "urgent" if: OVERDUE or URGENT REMINDER or past due, OR subject has BCD: with date, OR category is "po"
- "normal" if: category is "rfq", "quotation", or "finance"
- "low" if: category is "bounce", "internal", or "other"

Return ONLY the JSON object, nothing else."""

_TRIAGE_USER_TEMPLATE = """\
--- EMAIL ---
From: {sender}
Subject: {subject}

{body_text}
--- END EMAIL ---"""


def _ai_triage(email_dict: dict, tenant_id: Optional[str] = None) -> Optional[dict]:
    """
    Triage a single email using the Groq API (JSON mode via chat completion).
    Falls back to a rule-based default if AI fails.
    """
    body = (email_dict.get("body_text") or "")[:2000]
    user_msg = _TRIAGE_USER_TEMPLATE.format(
        sender=email_dict.get("sender", "unknown"),
        subject=email_dict.get("subject", "(No Subject)"),
        body_text=body,
    )

    try:
        result = chat_completion(
            messages=[
                {"role": "system", "content": _TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            purpose="email_triage",
            tenant_id=tenant_id,
            temperature=0.2,
            max_tokens=1024,
            json_mode=True,
        )
        if not result:
            fallback = _rule_based_triage(email_dict)
            fallback["_ai_used"] = False
            return fallback
        raw = result.content

        # Strip markdown fences if present
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

        data = json.loads(raw)

    except Exception as exc:
        logger.warning("AI triage failed for email %s: %s — using rule-based fallback",
                       email_dict.get("imap_uid", "?"), exc)
        fallback = _rule_based_triage(email_dict)
        fallback["_ai_used"] = False
        return fallback

    applied = _apply_rules(data, email_dict)
    applied["_ai_used"] = True
    return applied


def _rule_based_triage(email_dict: dict) -> dict:
    """Simple rule-based fallback when AI is unavailable."""
    subject = (email_dict.get("subject") or "").lower()
    body = (email_dict.get("body_text") or "").lower()
    combined = f"{subject} {body}"

    priority = "normal"
    needs_response = True

    if any(w in combined for w in ["urgent", "overdue", "past due", "asap", "immediately"]):
        priority = "urgent"
    elif any(w in combined for w in ["unsubscribe", "no-reply", "bounce", "mailer-daemon"]):
        priority = "low"
        needs_response = False

    return {
        "summary": f"Email from {email_dict.get('sender', 'unknown')}: {email_dict.get('subject', '')}",
        "priority": priority,
        "needs_response": needs_response,
        "category": "other",
        "sentiment": "neutral",
        "action_items": [],
        "suggested_reply": "",
    }


def _apply_rules(data: dict, email_dict: dict) -> dict:
    """Apply deterministic post-processing rules on top of AI output."""
    category = data.get("category", "other")
    combined = f"{email_dict.get('subject', '')} {email_dict.get('body_text', '')}"

    # Priority rules
    if _URGENT_PATTERNS.search(combined) or category in _URGENT_CATEGORIES:
        data["priority"] = "urgent"
    elif category in _NORMAL_CATEGORIES:
        data["priority"] = "normal"
    elif category in _LOW_CATEGORIES:
        data["priority"] = "low"

    # Validate priority
    if data.get("priority") not in _VALID_PRIORITIES:
        data["priority"] = "normal"

    # Validate sentiment
    if data.get("sentiment") not in _VALID_SENTIMENTS:
        data["sentiment"] = "neutral"

    # needs_response rules
    _, addr = parseaddr(str(email_dict.get("sender", "")).lower())
    is_noreply = any(addr.startswith(p) for p in ("mailer-daemon", "no-reply", "noreply"))
    if is_noreply or category in _RESPOND_FALSE:
        data["needs_response"] = False
    elif category in _RESPOND_TRUE:
        data["needs_response"] = True

    # Ensure action_items is a list
    if not isinstance(data.get("action_items"), list):
        data["action_items"] = []

    return data


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_and_triage(account: dict, scan_window_days: int | None = None) -> dict:
    """
    Full pipeline for one email account:
      1. Connect to IMAP
      2. Fetch recent emails inside the controlled scan window
      3. AI-triage each email
      4. Save new emails to Supabase (skips duplicates by imap_uid)
      5. Update last_synced_at on the account

    Args:
        account: Row from email_accounts table. Stored credentials are decrypted here.
        scan_window_days: Optional override for the policy's scan window.

    Returns:
        {"fetched": N, "saved": N, "failed": N}
    """
    account_id = account["id"]
    tenant_id = account["tenant_id"]
    email_addr = account["email"]
    policy = _get_sync_policy(tenant_id)
    if scan_window_days is not None:
        policy["scan_window_days"] = scan_window_days
    stats = {
        "fetched": 0,
        "saved": 0,
        "failed": 0,
        "skipped": 0,
        "ai_triaged": 0,
        "rule_triaged": 0,
        "policy": policy,
    }

    try:
        conn = _connect_imap(account)
        status, _ = conn.select("INBOX")
        if status != "OK":
            logger.error("IMAP SELECT failed for %s", email_addr)
            return stats

        since_date = (datetime.now(timezone.utc) - timedelta(days=policy["scan_window_days"] - 1)).strftime("%d-%b-%Y")
        # Search all recent inbox messages, not only UNSEEN. A user may already
        # have opened Gmail, but Orion should still brief today's inbox flow.
        status, uid_data = conn.uid("search", None, "SINCE", since_date)
        if status != "OK" or not uid_data[0] or not uid_data[0].strip():
            logger.info("No recent emails for %s since %s", email_addr, since_date)
            conn.logout()
            return stats

        all_uids = uid_data[0].split()
        uid_list = all_uids[-policy["max_emails_per_sync"]:]
        stats["fetched"] = len(uid_list)
        stats["skipped"] = max(0, len(all_uids) - len(uid_list))
        logger.info(
            "Found %d recent email(s) for %s since %s; processing latest %d by policy %s",
            len(all_uids),
            email_addr,
            since_date,
            len(uid_list),
            policy,
        )

        for uid in uid_list:
            try:
                fetch_status, msg_data = conn.uid("fetch", uid, "(RFC822)")
                if fetch_status != "OK" or not msg_data or msg_data[0] is None:
                    stats["failed"] += 1
                    continue

                raw_bytes = msg_data[0][1]
                if not isinstance(raw_bytes, bytes):
                    stats["failed"] += 1
                    continue

                parsed = _parse_raw_email(uid, raw_bytes, email_addr)

                # Cheap rules first, AI only for the most likely useful emails.
                rule_analysis = _rule_based_triage(parsed)
                if (
                    policy["auto_ai_triage"]
                    and stats["ai_triaged"] < policy["ai_triage_limit"]
                    and _should_ai_triage(parsed, rule_analysis)
                ):
                    analysis = _ai_triage(parsed, tenant_id=tenant_id)
                    if analysis and analysis.get("_ai_used"):
                        stats["ai_triaged"] += 1
                    else:
                        analysis = analysis or _apply_rules(rule_analysis, parsed)
                        stats["rule_triaged"] += 1
                else:
                    analysis = _apply_rules(rule_analysis, parsed)
                    stats["rule_triaged"] += 1

                # Merge and save to Supabase
                row = {
                    "tenant_id": tenant_id,
                    "account_id": account_id,
                    "imap_uid": parsed["imap_uid"],
                    "sender": parsed["sender"],
                    "subject": parsed["subject"],
                    "body_text": parsed["body_text"],
                    "received_at": parsed["received_at"],
                    "summary": analysis.get("summary", ""),
                    "priority": analysis.get("priority", "normal"),
                    "category": analysis.get("category", "other"),
                    "sentiment": analysis.get("sentiment", "neutral"),
                    "action_items": analysis.get("action_items", []),
                    "suggested_reply": analysis.get("suggested_reply", ""),
                    "needs_response": analysis.get("needs_response", False),
                    "responded": False,
                    "notified": False,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                }

                # Upsert — ignore if imap_uid already exists
                result = supabase.table("emails").upsert(
                    row,
                    on_conflict="tenant_id,imap_uid",
                    ignore_duplicates=True,
                ).execute()

                if result.data:
                    stats["saved"] += 1
                    logger.info("Saved email: %s | %s | priority=%s",
                                parsed["sender"], parsed["subject"], analysis.get("priority"))

            except Exception as exc:
                logger.exception("Error processing UID %s: %s", uid, exc)
                stats["failed"] += 1

        conn.logout()

        # Update last_synced_at
        supabase.table("email_accounts").update({
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
        log_event(
            "email_sync_completed",
            tenant_id=tenant_id,
            properties={
                "account_id": account_id,
                "fetched": stats["fetched"],
                "saved": stats["saved"],
                "failed": stats["failed"],
                "skipped": stats["skipped"],
                "ai_triaged": stats["ai_triaged"],
                "rule_triaged": stats["rule_triaged"],
            },
        )

    except (imaplib.IMAP4.error, OSError) as exc:
        logger.error("IMAP connection error for %s: %s", email_addr, exc)
        stats["failed"] = stats["fetched"]
        log_event(
            "email_sync_failed",
            tenant_id=tenant_id,
            properties={"account_id": account_id, "email": email_addr, "error": str(exc)[:200]},
        )

    logger.info("Sync complete for %s: %s", email_addr, stats)
    return stats


# ── SMTP send ─────────────────────────────────────────────────────────────────

def send_email(
    account: dict,
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
) -> bool:
    """
    Send a plain-text email via SMTP SSL using the account's credentials.

    Args:
        account:      Row from email_accounts. Stored credentials are decrypted here.
        to_address:   Recipient address.
        subject:      Email subject. "Re: " is prepended if replying.
        body:         Plain-text body.
        in_reply_to:  Message-ID of the original email (for threading).

    Returns:
        True on success, False on any error.
    """
    smtp_host = account.get("smtp_host", settings.default_smtp_host)
    smtp_port = account.get("smtp_port", settings.default_smtp_port)
    from_addr = account["email"]
    username = account.get("username") or from_addr
    password = _get_plain_credential(account)

    if not password:
        logger.error("No password for account %s", from_addr)
        return False

    _, recipient = parseaddr(to_address)
    recipient = recipient or to_address

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = MIMEMultipart()
    msg["From"]       = from_addr
    msg["To"]         = recipient
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"]  = in_reply_to

    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        if int(smtp_port) == 587:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls(context=ctx)
                if password.startswith("oauth2:"):
                    access_token = password.removeprefix("oauth2:")
                    try:
                        server.auth("XOAUTH2", lambda _: _xoauth2_string(from_addr, access_token))
                    except smtplib.SMTPAuthenticationError:
                        access_token = _refresh_google_access_token(account)
                        if not access_token:
                            raise
                        server.auth("XOAUTH2", lambda _: _xoauth2_string(from_addr, access_token))
                else:
                    server.login(username, password)
                server.sendmail(from_addr, recipient, msg.as_string())
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx) as server:
                if password.startswith("oauth2:"):
                    access_token = password.removeprefix("oauth2:")
                    try:
                        server.auth("XOAUTH2", lambda _: _xoauth2_string(from_addr, access_token))
                    except smtplib.SMTPAuthenticationError:
                        access_token = _refresh_google_access_token(account)
                        if not access_token:
                            raise
                        server.auth("XOAUTH2", lambda _: _xoauth2_string(from_addr, access_token))
                else:
                    server.login(username, password)
                server.sendmail(from_addr, recipient, msg.as_string())
        logger.info("Sent email from %s to %s | %s", from_addr, recipient, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP auth failed for %s", from_addr)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending from %s: %s", from_addr, exc)
        return False
    except OSError as exc:
        logger.error("Network error to %s:%s — %s", smtp_host, smtp_port, exc)
        return False
