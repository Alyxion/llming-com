"""Auto-generate FastAPI routes from a CommandRegistry.

Call ``build_command_router()`` to create an ``APIRouter`` where every
registered command becomes a REST endpoint.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from llming_com.command import (
    CommandDef,
    CommandError,
    CommandRegistry,
    CommandScope,
    INJECTED_PARAMS,
    get_default_command_registry,
)
from llming_com.session import BaseSessionRegistry

logger = logging.getLogger(__name__)


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
                if session_id == "current":
                    sessions = session_registry.list_sessions()
                    if not sessions:
                        raise HTTPException(404, "No active sessions")
                    session_id = max(sessions, key=lambda sid: sessions[sid].last_activity)
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
    register(path, tags=cmd.tags or None, summary=cmd.description)(_handler)
