"""
app/core/security.py
--------------------
JWT verification for Supabase-issued tokens.

Supabase uses ES256 (ECDSA) JWTs signed with a key pair. We verify them
by fetching the public JWKS from the Supabase auth endpoint.

For the /me and authenticated endpoints, we decode the token and extract
the user's sub (UUID) and any custom claims.

The backend service role key bypasses RLS — it does NOT verify tokens.
"""
import logging
from typing import Optional
from functools import lru_cache

import httpx
from jose import jwt, jwk, JWTError
from app.core.config import settings

logger = logging.getLogger(__name__)

# Supabase JWKS endpoint
JWKS_URL = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Fetch and cache the Supabase JWKS (JSON Web Key Set)."""
    try:
        resp = httpx.get(JWKS_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Fetched JWKS from %s (%d keys)", JWKS_URL, len(data.get("keys", [])))
        return data
    except Exception as exc:
        logger.error("Failed to fetch JWKS: %s", exc)
        return {"keys": []}


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and verify a Supabase JWT using the public JWKS.

    Returns the payload dict on success, None on any failure.
    The payload contains: sub (user_id), email, role, and any custom claims.
    """
    try:
        # Get the JWKS
        jwks_data = _fetch_jwks()

        # Decode the token header to find which key was used
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        alg = unverified_header.get("alg", "ES256")

        # Find the matching key
        signing_key = None
        for key_data in jwks_data.get("keys", []):
            if key_data.get("kid") == kid:
                signing_key = key_data
                break

        if not signing_key:
            logger.warning("No matching key found for kid=%s", kid)
            return None

        # Decode and verify the token
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
            audience="authenticated",
        )
        return payload

    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error decoding JWT: %s", exc)
        return None


def extract_user_id(payload: dict) -> Optional[str]:
    """Extract the user UUID from a decoded JWT payload."""
    return payload.get("sub")


def extract_tenant_id(payload: dict) -> Optional[str]:
    """
    Extract the tenant_id from JWT custom claims.
    We store tenant_id in the Supabase JWT via a custom hook (Phase 9).
    Until then, we look it up from the profiles table per request.
    """
    return payload.get("tenant_id") or payload.get("app_metadata", {}).get("tenant_id")
