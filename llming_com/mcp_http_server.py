"""HTTP/SSE MCP server mounted on a FastAPI/Starlette app.

Serves the command registry as MCP tools over HTTP/SSE transport.
Commands are resolved in-process — direct session/controller access.

Usage::

    from llming_com.mcp_http_server import mount_mcp_server

    mount_mcp_server(app, session_registry, prefix="/api/llming/mcp")
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Optional

from llming_com.command import (
    CommandError,
    CommandRegistry,
    CommandScope,
    INJECTED_PARAMS,
    get_default_command_registry,
)
from llming_com.session import BaseSessionRegistry

logger = logging.getLogger(__name__)


def mount_mcp_server(
    app,
    session_registry: BaseSessionRegistry,
    *,
    command_registry: Optional[CommandRegistry] = None,
    prefix: str = "/mcp",
    extras: Optional[dict] = None,
) -> None:
    """Mount an MCP HTTP/SSE server on a FastAPI/Starlette app.

    Args:
        app: FastAPI or Starlette application.
        session_registry: Session registry for session lookup.
        command_registry: Command registry (default: global).
        prefix: URL prefix for MCP endpoints.
        extras: Extra injectable values.
    """
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.types import TextContent, Tool

    cmd_registry = command_registry or get_default_command_registry()
    mcp = Server("llming-debug")
    extra_values = extras or {}

    def _active_app_type() -> str:
        """Detect app type from the most recently active session."""
        sessions = session_registry.list_sessions()
        if not sessions:
            return ""
        most_recent = max(sessions, key=lambda sid: sessions[sid].last_activity)
        return sessions[most_recent].app_type or ""

    @mcp.list_tools()
    async def list_tools() -> list[Tool]:
        app_filter = _active_app_type()
        tools = []
        for cmd in cmd_registry.list_commands(app_filter=app_filter):
            tools.append(Tool(
                name=cmd.name,
                description=cmd.description,
                inputSchema=cmd.input_schema(),
            ))
        return tools

    @mcp.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        cmd = cmd_registry.get(name)
        if not cmd:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown command: {name}"}))]

        try:
            inject: dict[str, Any] = {"registry": session_registry}
            inject.update(extra_values)

            if cmd.scope == CommandScope.SESSION:
                session_id = arguments.pop("session_id", "current")
                if session_id == "current":
                    sessions = session_registry.list_sessions()
                    if not sessions:
                        return [TextContent(type="text", text=json.dumps({"error": "No active sessions"}))]
                    session_id = max(sessions, key=lambda sid: sessions[sid].last_activity)
                entry = session_registry.get_session(session_id)
                if not entry:
                    return [TextContent(type="text", text=json.dumps({"error": f"Session {session_id} not found"}))]
                if cmd.requires_websocket and not entry.websocket:
                    return [TextContent(type="text", text=json.dumps({"error": "No WebSocket connected"}))]
                inject["session_id"] = session_id
                inject["entry"] = entry
                inject["controller"] = entry.controller

            # Build call kwargs
            sig = inspect.signature(cmd.handler)
            call_kwargs: dict[str, Any] = {}
            for pname in sig.parameters:
                if pname in inject:
                    call_kwargs[pname] = inject[pname]
                elif pname in arguments:
                    call_kwargs[pname] = arguments[pname]

            result = await cmd.handler(**call_kwargs)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return [TextContent(type="text", text=text)]

        except CommandError as e:
            return [TextContent(type="text", text=json.dumps({"error": e.detail, "status": e.status_code}))]
        except Exception as e:
            logger.exception("Command %s failed", name)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    # Mount SSE transport on the app
    sse = SseServerTransport(f"{prefix}/messages/")

    from starlette.routing import Mount, Route

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await mcp.run(streams[0], streams[1], mcp.create_initialization_options())

    # Add routes to the app
    app.routes.insert(0, Route(f"{prefix}/sse", endpoint=handle_sse))
    app.routes.insert(1, Mount(f"{prefix}/messages", app=sse.handle_post_message))

    logger.info("[MCP] HTTP/SSE server mounted at %s/sse", prefix)
