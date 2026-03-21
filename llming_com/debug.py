"""Base debug API router for llming applications.

Provides ``build_debug_router`` — creates FastAPI endpoints for inspecting
and controlling active sessions. Applications extend with domain-specific
endpoints via the ``extra_routes`` hook.

Protected by API key (header or query param) + optional IP whitelist.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from llming_com.session import BaseSessionEntry, BaseSessionRegistry

logger = logging.getLogger(__name__)


def _default_session_detail(session_id: str, entry: BaseSessionEntry) -> dict:
    """Default session detail — override via session_detail_hook."""
    return {}


def build_debug_router(
    registry: BaseSessionRegistry,
    *,
    api_key_env: str = "DEBUG_API_KEY",
    prefix: str = "/debug",
    allowed_networks: list[str] | None = None,
    session_detail_hook: Optional[
        Callable[[str, BaseSessionEntry], dict | Awaitable[dict]]
    ] = None,
    extra_routes: Optional[Callable[[APIRouter, BaseSessionRegistry], None]] = None,
) -> APIRouter:
    """Build a debug API router with session inspection endpoints.

    Provides:
        GET  {prefix}/sessions              — list all sessions
        GET  {prefix}/sessions/{id}         — session detail
        POST {prefix}/sessions/{id}/ws_send — forward a JSON message via WS

    Args:
        registry: The session registry to inspect.
        api_key_env: Environment variable name for the API key.
        prefix: URL prefix for all debug endpoints.
        allowed_networks: IP whitelist (CIDR notation). If None, allows
            127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16.
        session_detail_hook: Called with (session_id, entry) to add
            domain-specific fields to the session detail response.
            Can be sync or async.
        extra_routes: Called with (router, registry) to register
            additional domain-specific debug endpoints.
    """
    if allowed_networks is None:
        allowed_networks = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

    _skip_ip_check = "*" in allowed_networks
    _allowed = [] if _skip_ip_check else [ipaddress.ip_network(n) for n in allowed_networks]

    def _check_auth(request: Request) -> None:
        """Verify API key and IP whitelist."""
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise HTTPException(403, "Debug API not configured")

        # Check API key from header or query param
        provided = (
            request.headers.get("x-debug-key")
            or request.query_params.get("key")
            or ""
        )
        if not hmac.compare_digest(provided, api_key):
            raise HTTPException(401, "Invalid API key")

        # IP whitelist
        client_ip = request.client.host if request.client else None
        if not _skip_ip_check and client_ip:
            try:
                addr = ipaddress.ip_address(client_ip)
                if not any(addr in net for net in _allowed):
                    raise HTTPException(403, f"IP {client_ip} not allowed")
            except ValueError:
                raise HTTPException(403, f"Invalid client IP: {client_ip}")

    router = APIRouter(prefix=prefix, dependencies=[Depends(_check_auth)])

    # ── List sessions ─────────────────────────────────────────
    @router.get("/sessions")
    async def list_sessions():
        sessions = registry.list_sessions()
        now = time.monotonic()
        result = []
        for sid, entry in sessions.items():
            result.append({
                "session_id": sid,
                "user_id": entry.user_id,
                "user_name": entry.user_name,
                "ws_connected": entry.websocket is not None,
                "idle_seconds": round(now - entry.last_activity),
                "created_seconds_ago": round(now - entry.created_at),
            })
        return {"count": len(result), "sessions": result}

    # ── Session detail ────────────────────────────────────────
    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        entry = registry.get_session(session_id)
        if not entry:
            raise HTTPException(404, f"Session {session_id} not found")

        now = time.monotonic()
        result = {
            "session_id": session_id,
            "user_id": entry.user_id,
            "user_name": entry.user_name,
            "user_email": entry.user_email,
            "ws_connected": entry.websocket is not None,
            "idle_seconds": round(now - entry.last_activity),
            "created_seconds_ago": round(now - entry.created_at),
        }

        # Domain-specific detail
        if session_detail_hook:
            import asyncio
            extra = session_detail_hook(session_id, entry)
            if asyncio.iscoroutine(extra):
                extra = await extra
            if isinstance(extra, dict):
                result.update(extra)

        return result

    # ── Forward WS message ────────────────────────────────────
    @router.post("/sessions/{session_id}/ws_send")
    async def ws_send(session_id: str, request: Request):
        """Forward an arbitrary JSON message through the session's WS handler."""
        entry = registry.get_session(session_id)
        if not entry:
            raise HTTPException(404, f"Session {session_id} not found")
        if not entry.controller:
            raise HTTPException(400, "Session has no controller")

        data = await request.json()
        await entry.controller.handle_message(data)
        return {"ok": True, "forwarded": data.get("type", "")}

    # ── Extra routes ──────────────────────────────────────────
    if extra_routes:
        extra_routes(router, registry)

    return router
