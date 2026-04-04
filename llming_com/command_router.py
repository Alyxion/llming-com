"""Auto-generate FastAPI routes from a CommandRegistry.

Call ``build_command_router()`` to create an ``APIRouter`` where every
registered command becomes a REST endpoint.

CSRF note: This router is designed for use behind cookie-based auth.
When deployed with ``SameSite=Lax`` cookies (the default), the browser
will not send cookies on cross-origin POST requests, which provides
baseline CSRF protection. For additional safety, callers can add a
custom CSRF token header check via ``auth_dependency``.
"""

from __future__ import annotations

import enum
import inspect
import logging
from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from llming_com.auth import get_auth_session_id
from llming_com.command import (
    CommandDef,
    CommandError,
    CommandRegistry,
    CommandScope,
    get_default_command_registry,
)
from llming_com.session import BaseSessionRegistry

logger = logging.getLogger(__name__)


def _coerce_param(value: Any, target_type: type) -> Any:
    """Basic parameter type coercion from query string values."""
    if value is None:
        return value
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is bool:
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if target_type is str:
        return str(value)
    return value


def build_command_router(
    session_registry: BaseSessionRegistry,
    *,
    command_registry: Optional[CommandRegistry] = None,
    auth_dependency: Any = None,
    prefix: str = "/debug",
    extras: Optional[dict] = None,
) -> APIRouter:
    """Build a FastAPI router from all registered commands.

    Args:
        session_registry: Session registry for session lookup.
        command_registry: Command registry (default: global registry).
        auth_dependency: FastAPI ``Depends`` callable for auth.
        prefix: URL prefix for all routes.
        extras: Extra injectable values (e.g. ``nudge_store``).

    Returns:
        Configured ``APIRouter``.
    """
    cmd_registry = command_registry or get_default_command_registry()
    deps = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(prefix=prefix, dependencies=deps)

    # Meta-endpoint: list all registered commands
    @router.get("/commands")
    async def list_commands(app: str = ""):
        """List registered commands. Pass ``?app=lodge`` to filter by app type."""
        return {"commands": [c.to_dict() for c in cmd_registry.list_commands(app_filter=app)]}

    # Mount each command
    for cmd in cmd_registry.list_commands():
        _mount_command(router, cmd, session_registry, extras or {})

    return router


def _mount_command(
    router: APIRouter,
    cmd: CommandDef,
    session_registry: BaseSessionRegistry,
    extras: dict,
) -> None:
    """Mount a single command as a FastAPI route."""

    # Build a type map for parameter coercion
    _param_types: dict[str, type] = {p.name: p.type for p in cmd.params}

    # Determine URL path
    if cmd.http_path:
        path = cmd.http_path
    elif cmd.scope == CommandScope.GLOBAL:
        path = f"/{cmd.name}"
    else:
        path = f"/sessions/{{session_id}}/{cmd.name}"

    async def _handler(request: Request, session_id: str = ""):
        try:
            # Build injection context
            inject: dict[str, Any] = {"request": request, "registry": session_registry}
            inject.update(extras)

            if cmd.scope == CommandScope.SESSION:
                if not session_id:
                    raise HTTPException(400, "session_id required")
                # Resolve "current" to the most recently active session
                # scoped to the authenticated user
                if session_id == "current":
                    auth_sid = get_auth_session_id(request)
                    sessions = session_registry.list_sessions()
                    if not sessions:
                        raise HTTPException(404, "No active sessions")
                    # If we have an authenticated user, prefer their session
                    candidates = sessions
                    if auth_sid and auth_sid in sessions:
                        candidates = {auth_sid: sessions[auth_sid]}
                    session_id = max(candidates, key=lambda sid: candidates[sid].last_activity)
                entry = session_registry.get_session(session_id)
                if not entry:
                    raise HTTPException(404, f"Session {session_id} not found")
                if cmd.requires_websocket and not entry.websocket:
                    raise HTTPException(409, "No WebSocket connected")
                inject["session_id"] = session_id
                inject["entry"] = entry
                inject["controller"] = entry.controller

            # Extract user params from body or query
            user_params: dict[str, Any] = {}
            if cmd.http_method in ("POST", "PUT", "DELETE"):
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        user_params.update(body)
                except Exception:
                    pass
            for p in cmd.params:
                qval = request.query_params.get(p.name)
                if qval is not None:
                    user_params[p.name] = qval

            # Apply type coercion for query params
            for pname, pval in user_params.items():
                if pname in _param_types and isinstance(pval, str):
                    try:
                        user_params[pname] = _coerce_param(pval, _param_types[pname])
                    except (ValueError, TypeError):
                        pass

            # Build call kwargs from signature
            sig = inspect.signature(cmd.handler)
            call_kwargs: dict[str, Any] = {}
            for pname in sig.parameters:
                if pname in inject:
                    call_kwargs[pname] = inject[pname]
                elif pname in user_params:
                    call_kwargs[pname] = user_params[pname]

            return await cmd.handler(**call_kwargs)

        except CommandError as e:
            raise HTTPException(e.status_code, e.detail)

    # Register with the appropriate HTTP method
    method_map = {"GET": router.get, "POST": router.post, "PUT": router.put, "DELETE": router.delete}
    register = method_map.get(cmd.http_method, router.post)
    register(path, tags=cast(list[str | enum.Enum] | None, cmd.tags or None), summary=cmd.description)(_handler)
