"""Server-verified identity. Demo access exists only when explicitly enabled."""
from __future__ import annotations

import hmac
import httpx
from fastapi import Header, HTTPException
from .settings import get_settings


def authenticated_user_id(authorization: str | None) -> str:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(503, "Account service is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please sign in.")
    try:
        response = httpx.get(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
            headers={"apikey": settings.supabase_service_role_key, "Authorization": authorization}, timeout=10,
        )
    except httpx.HTTPError as error:
        raise HTTPException(503, "Account service is temporarily unavailable.") from error
    if response.status_code in (401, 403):
        raise HTTPException(401, "Your session expired. Please sign in again.")
    if response.status_code != 200:
        raise HTTPException(503, "Account service is temporarily unavailable.")
    user_id = response.json().get("id")
    if not user_id:
        raise HTTPException(401, "Invalid session.")
    return str(user_id)


def owner(authorization: str | None = Header(default=None)) -> str:
    if get_settings().demo_mode:
        return "demo-browser"
    return f"account:{authenticated_user_id(authorization)}"


def require_internal(x_lodestone_internal: str | None = Header(default=None)) -> None:
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(503, "Internal scheduler is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(expected, x_lodestone_internal):
        raise HTTPException(401, "Unauthorized.")
