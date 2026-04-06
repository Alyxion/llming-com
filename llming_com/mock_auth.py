"""Mock authentication for headless testing.

Provides:
- A registry of mock user profiles (keyed by email)
- A ``/mock-login`` endpoint that bypasses OAuth entirely
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

if TYPE_CHECKING:
    from office_mcp.testing.mock_data import MockUserProfile

logger = logging.getLogger(__name__)

# ── Mock user registry ───────────────────────────────────────────

_mock_sessions: dict[str, "MockUserProfile"] = {}


def register_mock_user(email: str, profile: "MockUserProfile") -> None:
    """Register a mock user (called at app startup)."""
    _mock_sessions[email.lower()] = profile


def get_mock_profile(email: str) -> "MockUserProfile | None":
    """Look up a registered mock profile by email."""
    return _mock_sessions.get(email.lower())


def is_registered_mock_user(email: str) -> bool:
    return email.lower() in _mock_sessions


# ── Check mock enabled ───────────────────────────────────────────

def _is_mock_enabled() -> bool:
    """Check if mock users are enabled at import time."""
    try:
        from office_mcp.testing import is_mock_enabled
        return is_mock_enabled()
    except ImportError:
        return False


# ── FastAPI router ───────────────────────────────────────────────

def build_mock_login_router() -> APIRouter:
    """Build a router with ``GET /mock-login?email=...``.

    If mock users are not enabled (``LLMING_MOCK_USERS=1``), returns an
    empty router with no endpoints -- the route is never registered.

    The endpoint:
    1. Creates an ``MsGraphInstance`` with mock transport enabled
    2. Acquires a synthetic token (no Azure AD call)
    3. Populates Redis with the mock profile (real Redis, mock data)
    4. Sets ``llming_identity`` + ``llming_auth`` cookies
    5. Redirects to ``/chat``
    """
    router = APIRouter()

    # Gate at router creation time -- return empty router if not enabled
    if not _is_mock_enabled():
        return router

    @router.get("/mock-login")
    async def mock_login(email: str, request: Request):
        profile = get_mock_profile(email)
        if not profile:
            return JSONResponse({"error": f"Unknown mock user: {email}"}, status_code=404)

        # ── Create graph instance with mock transport ────────
        from office_mcp.msgraph.ms_graph_handler import MsGraphInstance

        session_id = f"mock-session-{secrets.token_hex(16)}"
        graph = MsGraphInstance(
            scopes=["User.Read"],
            app=os.environ.get("O365_SERVER_NAME", os.environ.get("O365_APP_NAME", "llming_app")),
            redis_url=os.environ.get("O365_REDIS_URL"),
            mongodb_url=os.environ.get("O365_MONGODB_URL"),
            session_id=session_id,
        )
        graph.ensure_unique_user_id()
        graph.enable_mock(profile)

        # ── Acquire token (hits mock _async_token_request) ───
        from office_mcp.testing.mock_tokens import make_mock_token_response
        token_resp = make_mock_token_response(profile.email, profile.user_id)
        await graph.set_access_token_async(token_resp["access_token"])
        await graph.set_refresh_token_async(token_resp["refresh_token"])

        # ── Fetch profile (hits mock transport) ──────────────
        await graph.get_profile_async()
        await graph.cache_profile_to_redis_async()

        # ── Set cookies + redirect ───────────────────────────
        from llming_com.auth import get_auth as _auth

        auth = _auth()
        response = RedirectResponse(url="/chat", status_code=302)
        identity_token = auth.sign_identity_token(session_id)
        response.set_cookie(
            "llming_identity", identity_token,
            httponly=True, samesite="lax", secure=True, max_age=7 * 24 * 3600,
        )
        auth_token = auth.sign_auth_token(session_id)
        response.set_cookie(
            "llming_auth", auth_token,
            httponly=True, samesite="lax", secure=True, max_age=7 * 24 * 3600,
        )

        logger.info("[MOCK] Login successful for %s (session=%s)", profile.email, session_id[:8])
        return response

    return router
