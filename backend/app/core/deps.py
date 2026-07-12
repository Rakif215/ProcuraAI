"""
app/core/deps.py
----------------
FastAPI dependency injection functions.
These are used with Depends() in route handlers.
"""
import logging
from typing import Annotated, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, extract_user_id
from app.db.client import get_db, supabase

logger = logging.getLogger(__name__)

# HTTP Bearer token extractor
bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    """Represents an authenticated user with their tenant context."""

    def __init__(self, user_id: str, tenant_id: str, email: str, role: str = "member"):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.email = email
        self.role = role

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """
    Extract and validate the JWT from the Authorization header.
    Look up the user's profile to get tenant_id and role.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = extract_user_id(payload)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    # Fetch the profile to get tenant_id and role
    try:
        result = supabase.table("profiles").select("tenant_id, role").eq("id", user_id).single().execute()
        profile = result.data
    except Exception as exc:
        logger.error("Failed to fetch profile for user %s: %s", user_id, exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User profile not found")

    return CurrentUser(
        user_id=user_id,
        tenant_id=profile["tenant_id"],
        email=payload.get("email", ""),
        role=profile.get("role", "member"),
    )


async def get_admin_user(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Require the user to be an admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# Convenient type aliases for route handlers
DBSession = Annotated[AsyncSession, Depends(get_db)]
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
AdminUser = Annotated[CurrentUser, Depends(get_admin_user)]
