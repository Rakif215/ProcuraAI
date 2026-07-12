"""
app/api/v1/auth.py
------------------
Authentication routes — register, login, logout, me.
All actual auth is handled by Supabase; we're just a thin proxy
that also ensures a profile row exists for new users.
"""
import logging
import re
from uuid import uuid4
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.db.client import supabase, get_supabase_anon
from app.core.deps import AuthUser
from app.core.config import settings
from app.services.telemetry import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _friendly_auth_error(exc: Exception) -> HTTPException:
    message = str(exc)
    lowered = message.lower()
    if (
        "nodename nor servname" in lowered
        or "could not resolve" in lowered
        or "connecterror" in lowered
        or "name resolution" in lowered
        or "temporary failure in name resolution" in lowered
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orion cannot reach Supabase right now. Check internet/DNS and try again.",
        )
    if "user not allowed" in lowered or "already" in lowered or "exists" in lowered:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please sign in instead.",
        )
    return HTTPException(status_code=500, detail=message)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "workspace")[:42]


def _create_workspace_for_user(user_id: str, email: str, full_name: str, company_name: str) -> tuple[str, str]:
    """Create tenant-owned defaults for a user and return tenant_id, role."""
    slug = f"{_slugify(company_name)}-{uuid4().hex[:8]}"

    tenant_result = supabase.table("tenants").insert({
        "name": company_name,
        "slug": slug,
        "plan": "starter",
    }).execute()
    tenant_id = tenant_result.data[0]["id"]

    supabase.table("profiles").insert({
        "id": user_id,
        "tenant_id": tenant_id,
        "full_name": full_name or email.split("@")[0],
        "role": "admin",
    }).execute()

    supabase.table("subscriptions").insert({
        "tenant_id": tenant_id,
        "plan": "starter",
        "credits_limit": 1000,
    }).execute()

    supabase.table("agents").insert({
        "tenant_id": tenant_id,
        "name": "Orion Assistant",
        "description": "Your default AI business assistant",
        "system_prompt": (
            "You are Orion, a professional AI business assistant for "
            f"{company_name}. You help manage emails, track tasks, "
            "and coordinate communications efficiently. Be concise, "
            "professional, and action-oriented."
        ),
        "tools_enabled": ["get_urgent_emails", "get_unread_emails", "search_emails", "get_email_detail", "web_search"],
        "is_active": True,
    }).execute()

    return tenant_id, "admin"


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    password: str
    full_name: str


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    username: str = ""
    tenant_id: str
    role: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """
    Register a new user and create their company workspace (tenant).
    Flow:
      1. Create Supabase auth user (auto-confirm in dev)
      2. Create tenant row
      3. Create profile row linking user ↔ tenant
      4. Create default agent
      5. Sign in to get JWT
    """
    current_step = "init"
    user_id = ""
    username = body.username.strip().lower()
    email = f"{username}@orion.local"
    company_name = f"{body.full_name}'s Workspace"
    try:
        # 1. Create auth user via admin API (auto-confirmed)
        current_step = "create_user"
        try:
            user = supabase.auth.admin.create_user({
                "email": email,
                "password": body.password,
                "email_confirm": True,
            })
        except Exception as exc:
            raise _friendly_auth_error(exc)

        if not user.user:
            raise HTTPException(status_code=400, detail="Registration failed — invalid username or password")

        user_id = user.user.id

        # 2-5. Create tenant, profile, subscription, and default agent.
        current_step = "create_workspace"
        tenant_id, role = _create_workspace_for_user(
            user_id=user_id,
            email=email,
            full_name=body.full_name,
            company_name=company_name,
        )

        # 6. Sign in to get a JWT
        current_step = "signin"
        auth_response = get_supabase_anon().auth.sign_in_with_password({
            "email": email,
            "password": body.password,
        })
        access_token = auth_response.session.access_token if auth_response.session else ""

        logger.info("New tenant registered: %s (user=%s)", company_name, user_id)
        log_event(
            "auth_register_completed",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"username": username},
        )

        return AuthResponse(
            access_token=access_token,
            user_id=user_id,
            email=email,
            username=username,
            tenant_id=tenant_id,
            role=role,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Registration error at step %s: %s", current_step, exc, exc_info=True)
        if user_id and current_step != "create_user":
            try:
                supabase.auth.admin.delete_user(user_id)
                logger.info("Cleaned up auth user %s after registration failure", user_id)
            except Exception as cleanup_exc:
                logger.error("Failed to clean up auth user %s: %s", user_id, cleanup_exc)
        friendly = _friendly_auth_error(exc)
        if friendly.status_code != 500:
            raise friendly
        raise HTTPException(status_code=500, detail=f"Registration failed [{current_step}]: {str(exc)}")


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest):
    """
    Log in with email + password. Returns Supabase JWT.
    """
    raw_username = body.username.strip().lower()
    # Backward compat: if user types an email (legacy account), use it directly
    if "@" in raw_username:
        email = raw_username
        username = email.split("@")[0]
    else:
        username = raw_username
        email = f"{username}@orion.local"
    try:
        auth_response = get_supabase_anon().auth.sign_in_with_password({
            "email": email,
            "password": body.password,
        })

        if not auth_response.user or not auth_response.session:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        user_id = auth_response.user.id

        # Fetch profile for tenant_id and role. If an earlier interrupted signup left
        # an auth user without app rows, repair it into a default workspace.
        try:
            profile = supabase.table("profiles").select("tenant_id, role").eq("id", user_id).single().execute()
            profile_data = profile.data
        except Exception:
            tenant_id, role = _create_workspace_for_user(
                user_id=user_id,
                email=email,
                full_name=username,
                company_name=f"{username}'s Workspace",
            )
            profile_data = {"tenant_id": tenant_id, "role": role}

        log_event(
            "auth_login_completed",
            tenant_id=profile_data["tenant_id"],
            user_id=user_id,
            properties={"username": username},
        )

        return AuthResponse(
            access_token=auth_response.session.access_token,
            user_id=user_id,
            email=email,
            username=username,
            tenant_id=profile_data["tenant_id"],
            role=profile_data.get("role", "member"),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Login error for %s: %s", username, exc, exc_info=True)
        log_event("auth_login_failed", properties={"username": username, "error": str(exc)[:120]})
        friendly = _friendly_auth_error(exc)
        if friendly.status_code != 500:
            raise friendly
        raise HTTPException(status_code=401, detail=f"Login failed: {str(exc)}")


@router.get("/me")
async def get_me(current_user: AuthUser):
    """Return the currently authenticated user's info."""
    return {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "tenant_id": current_user.tenant_id,
        "role": current_user.role,
    }


@router.post("/logout")
async def logout(current_user: AuthUser):
    """Sign out (Supabase invalidates the token server-side)."""
    try:
        supabase.auth.sign_out()
    except Exception:
        pass  # best-effort
    return {"message": "Logged out successfully"}
