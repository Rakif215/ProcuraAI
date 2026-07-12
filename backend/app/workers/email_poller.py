"""
app/workers/email_poller.py
-----------------------------
Celery background task: poll all active email accounts for new mail.

This runs every 5 minutes by default (configured in celeryconfig.py).
It fetches all active email accounts for all tenants, then calls
fetch_and_triage() for each one — which connects to IMAP, fetches unread
emails, AI-triages them, and saves them to Supabase.

Usage:
  # Start a Celery worker (from backend/)
  celery -A app.workers.celery_app worker --loglevel=info

  # Start the beat scheduler (to run on a schedule)
  celery -A app.workers.celery_app beat --loglevel=info
"""
import logging

from app.workers.celery_app import celery_app
from app.db.client import supabase
from app.services.email_service import fetch_and_triage

logger = logging.getLogger(__name__)


@celery_app.task(
    name="email.poll_all",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 min between retries
    soft_time_limit=300,     # 5 min max per task
)
def poll_all_accounts(self):
    """
    Fetch and triage emails for all active tenant accounts.
    Called by Celery Beat every 5 minutes.
    """
    logger.info("Starting email poll for all tenants")

    try:
        # Fetch all active email accounts
        result = (
            supabase.table("email_accounts")
            .select("id, tenant_id, email, username, imap_host, imap_port, smtp_host, smtp_port, password_encrypted")
            .eq("is_active", True)
            .execute()
        )
        accounts = result.data or []
        logger.info("Polling %d active account(s)", len(accounts))

        total_stats = {"fetched": 0, "saved": 0, "failed": 0}

        for account in accounts:
            if not account.get("password_encrypted"):
                logger.warning("Skipping account %s — no password configured", account.get("email"))
                continue

            try:
                stats = fetch_and_triage(account)
                for k in total_stats:
                    total_stats[k] += stats.get(k, 0)
            except Exception as exc:
                logger.error("Failed to poll account %s: %s", account.get("email"), exc)
                total_stats["failed"] += 1

        logger.info("Poll complete: %s", total_stats)
        return total_stats

    except Exception as exc:
        logger.error("poll_all_accounts failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


@celery_app.task(
    name="email.poll_account",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def poll_single_account(self, account_id: str, scan_window_days: int | None = None):
    """
    Poll a single email account by ID.
    Used when a user manually triggers a sync from the Settings page.
    """
    logger.info("Polling single account: %s (scan_window_override=%s)", account_id, scan_window_days)

    try:
        result = (
            supabase.table("email_accounts")
            .select("id, tenant_id, email, username, imap_host, imap_port, smtp_host, smtp_port, password_encrypted")
            .eq("id", account_id)
            .single()
            .execute()
        )
        account = result.data
        if not account:
            logger.warning("Account %s not found", account_id)
            return {"error": "Account not found"}

        if not account.get("password_encrypted"):
            return {"error": "No email password configured"}

        stats = fetch_and_triage(account, scan_window_days=scan_window_days)
        return stats

    except Exception as exc:
        logger.error("poll_single_account failed for %s: %s", account_id, exc)
        raise self.retry(exc=exc)
