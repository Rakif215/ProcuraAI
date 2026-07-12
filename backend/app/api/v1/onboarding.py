"""
app/api/v1/onboarding.py
-------------------------
Streamlined onboarding for Orion Lite.

Two paths:
  1. Gmail → Google OAuth2 → XOAUTH2 for IMAP/SMTP (no passwords)
  2. Custom IMAP → encrypted password storage

The onboarding flow:
  Step 1: User connects a mailbox (SpaceMail/custom IMAP first; Gmail OAuth optional)
  Step 2: User chooses the first sync window for recent-email personalization
  Step 3: User sets profile (role, tone, email_length)
  Step 4: We start syncing their inbox
"""
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.config import settings
from app.core.deps import AuthUser
from app.core.crypto import encrypt_password
from app.db.client import supabase
from app.services.provider_discovery import discover_provider_config
from app.services.telemetry import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Google OAuth2 endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Scopes: email + IMAP/SMTP access
GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://mail.google.com/",  # Full IMAP/SMTP access
]

# ── Schemas ───────────────────────────────────────────────────────────────────

class ProfileSetup(BaseModel):
    role: str = "founder"
    tone: str = "professional"
    email_length: str = "medium"
    industry: Optional[str] = None
    timezone: str = "Asia/Qatar"
    initial_sync_window_days: int = 1


class ManualEmailSetup(BaseModel):
    email: str
    password: str
    username: Optional[str] = None
    provider: str = "custom"
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None


class ProviderDiscoveryRequest(BaseModel):
    email: str
    provider_hint: Optional[str] = None


@router.post("/provider-discovery")
async def provider_discovery(body: ProviderDiscoveryRequest):
    config = discover_provider_config(body.email, body.provider_hint)
    return {"config": config.to_dict()}


# ── Google OAuth2 ─────────────────────────────────────────────────────────────

@router.get("/google/connect")
async def google_connect(user: AuthUser):
    """
    Step 1: Redirect user to Google consent screen.
    The frontend opens this URL in a popup or redirect.
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",       # Get refresh_token
        "prompt": "consent",            # Force consent to ensure refresh_token
        "state": user.user_id,          # Pass user_id through OAuth flow
    }

    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    log_event("onboarding_mailbox_connect_started", tenant_id=user.tenant_id, user_id=user.user_id, properties={"provider": "gmail"})
    return {"auth_url": url}


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),  # user_id passed from connect
):
    """
    Step 2: Google redirects here with an auth code.
    We exchange it for access_token + refresh_token,
    then save the email account with OAuth tokens.
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")

    # Exchange auth code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("Google token exchange failed: %s", token_resp.text)
            raise HTTPException(status_code=400, detail="Failed to authenticate with Google")

        tokens = token_resp.json()
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")

        # Get user's email from Google
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get Google user info")

        userinfo = userinfo_resp.json()
        email = userinfo["email"]

    user_id = state
    now = datetime.now(timezone.utc).isoformat()

    # Look up tenant_id from user profile
    try:
        profile = supabase.table("profiles").select("tenant_id").eq("id", user_id).single().execute()
        tenant_id = profile.data["tenant_id"]
    except Exception:
        raise HTTPException(status_code=404, detail="User profile not found")

    # Save email account with OAuth tokens (encrypted)
    account_data = {
        "tenant_id": tenant_id,
        "email": email,
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "password_encrypted": encrypt_password(f"oauth2:{access_token}"),
        "oauth_refresh_token": encrypt_password(refresh_token) if refresh_token else None,
        "is_active": True,
        "last_synced_at": None,
    }

    try:
        result = supabase.table("email_accounts").upsert(
            account_data,
            on_conflict="tenant_id,email",
        ).execute()
        account_id = result.data[0]["id"] if result.data else None
        log_event(
            "onboarding_mailbox_connected",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"provider": "gmail", "account_id": account_id, "email": email},
        )

    except Exception as exc:
        logger.error("Failed to save Gmail account: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save email account")

    # Redirect to frontend success page
    frontend = settings.frontend_url
    return RedirectResponse(
        url=f"{frontend}/onboarding/success?email={email}&account_id={account_id}"
    )


# ── Manual IMAP Setup (non-Gmail) ────────────────────────────────────────────

@router.post("/email/connect")
async def connect_email_manual(body: ManualEmailSetup, user: AuthUser):
    """
    Connect an email account via manual IMAP credentials.
    For non-Gmail providers (SpaceMail, Outlook, custom).
    """
    # Backend discovery decides defaults. The frontend should not hardcode these.
    discovered = discover_provider_config(body.email, body.provider)
    imap_host = body.imap_host or discovered.imap_host or settings.default_imap_host
    imap_port = body.imap_port or discovered.imap_port or 993
    smtp_host = body.smtp_host or discovered.smtp_host or settings.default_smtp_host
    smtp_port = body.smtp_port or discovered.smtp_port or 465

    # Test IMAP connection before saving
    import imaplib
    try:
        if imap_port == 993:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        else:
            conn = imaplib.IMAP4(imap_host, imap_port)
        conn.login(body.username or body.email, body.password)
        conn.logout()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not connect to {imap_host}: {str(exc)[:200]}"
        )

    # Save with encrypted password
    account_data = {
        "tenant_id": user.tenant_id,
        "email": body.email,
        "username": body.username,
        "provider": body.provider,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "password_encrypted": encrypt_password(body.password),
        "is_active": True,
    }

    try:
        result = supabase.table("email_accounts").upsert(
            account_data,
            on_conflict="tenant_id,email",
        ).execute()
        account_id = result.data[0]["id"] if result.data else None
        log_event(
            "onboarding_mailbox_connected",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            properties={"provider": body.provider, "account_id": account_id, "email": body.email},
        )

        return {
            "status": "connected",
            "account_id": account_id,
            "email": body.email,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Profile Setup ─────────────────────────────────────────────────────────────

@router.post("/profile")
async def setup_profile(body: ProfileSetup, user: AuthUser, background_tasks: BackgroundTasks):
    """
    Save the user's initial profile during onboarding.
    Maps to user_profiles table (memory layer).
    """
    now = datetime.now(timezone.utc).isoformat()

    profile_data = {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "role": body.role,
        "industry": body.industry,
        "timezone": body.timezone,
        "tone": body.tone,
        "email_length": body.email_length,
        "preferences": {
            "sync_policy": {
                "scan_window_days": max(1, min(body.initial_sync_window_days or 1, 7)),
            }
        },
        "updated_at": now,
    }

    try:
        result = supabase.table("user_profiles").upsert(
            profile_data,
            on_conflict="user_id",
        ).execute()
        log_event(
            "onboarding_profile_saved",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            properties={
                "role": body.role,
                "tone": body.tone,
                "email_length": body.email_length,
                "initial_sync_window_days": max(1, min(body.initial_sync_window_days or 1, 7)),
            },
        )
        log_event(
            "onboarding_first_sync_window_set",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            properties={"days": max(1, min(body.initial_sync_window_days or 1, 7))},
        )

        initial_sync_started = False
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
                background_tasks.add_task(fetch_and_triage, account)
                initial_sync_started = True
        except Exception as sync_exc:
            logger.warning("Initial onboarding sync skipped for %s: %s", user.user_id, sync_exc)

        return {
            "status": "saved",
            "profile": result.data[0] if result.data else profile_data,
            "initial_sync_started": initial_sync_started,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Status Check ──────────────────────────────────────────────────────────────

@router.get("/status")
async def onboarding_status(user: AuthUser):
    """Check if user has completed onboarding (email connected + profile set)."""
    has_email = False
    has_profile = False

    try:
        accounts = (
            supabase.table("email_accounts")
            .select("id, email, is_active, last_synced_at")
            .eq("tenant_id", user.tenant_id)
            .eq("is_active", True)
            .execute()
        )
        has_email = bool(accounts.data)
    except Exception:
        pass

    try:
        profile = (
            supabase.table("user_profiles")
            .select("role, tone")
            .eq("user_id", user.user_id)
            .single()
            .execute()
        )
        has_profile = bool(profile.data and profile.data.get("role"))
    except Exception:
        pass

    return {
        "is_complete": has_email and has_profile,
        "has_email": has_email,
        "has_profile": has_profile,
        "accounts": accounts.data if has_email else [],
    }


# ── Provider Presets ──────────────────────────────────────────────────────────

@router.get("/providers")
async def list_providers():
    """Return available email provider presets for the frontend dropdown."""
    return {
        "providers": [
            {"id": "gmail", "name": "Gmail", "method": "oauth", "icon": "mail"},
            {"id": "fau", "name": "FAU / RRZE Exchange", "method": "password", "icon": "mail"},
            {"id": "outlook", "name": "Outlook / Microsoft", "method": "password", "icon": "mail"},
            {"id": "spacemail", "name": "SpaceMail", "method": "password", "icon": "mail"},
            {"id": "custom", "name": "Manual advanced setup", "method": "password", "icon": "settings"},
        ]
    }
