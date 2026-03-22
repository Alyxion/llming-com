"""Stdio MCP server that proxies commands to a running llming app via HTTP.

For environments requiring stdio transport (e.g. Claude Cowork).

Config for ``.mcp.json``::

    {
        "mcpServers": {
            "llming-debug": {
                "command": "python",
                "args": ["-m", "llming_com.mcp_stdio_server"],
                "env": {
                    "LLMING_DEBUG_URL": "http://localhost:8080/api/llming/debug",
                    "LLMING_DEBUG_KEY": "your-api-key"
                }
            }
        }
    }

Startup flow:
1. Starts immediately with zero tools
2. Polls ``GET /commands`` until the app is reachable
3. Sends ``notifications/tools/list_changed`` once commands discovered
4. Tool calls are proxied as HTTP requests to the debug API
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


async def _http_request(method: str, url: str, api_key: str,
                        body: dict | None = None, params: dict | None = None) -> dict:
    """Make an authenticated HTTP request."""
    import aiohttp
    headers = {"x-debug-key": api_key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers,
                                   json=body, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                text = await resp.text()
                return {"error": f"HTTP {resp.status}: {text[:200]}"}
            return await resp.json()


async def main():
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    base_url = os.environ.get("LLMING_DEBUG_URL", "http://localhost:8080/api/llming/debug")
    api_key = os.environ.get("LLMING_DEBUG_KEY", "")

    if not api_key:
        print("LLMING_DEBUG_KEY not set", file=sys.stderr)
        sys.exit(1)

    server = Server("llming-debug-stdio")
    commands: list[dict] = []
    tools_ready = asyncio.Event()

    async def discover_commands():
        """Poll until the app is reachable and commands are discovered."""
        nonlocal commands
        while True:
            try:
                data = await _http_request("GET", f"{base_url}/commands", api_key)
                commands = data.get("commands", [])
                if commands:
                    tools_ready.set()
                    # Notify client that tools changed
                    await server.request_context.session.send_notification(
                        "notifications/tools/list_changed", {}
                    )
                    logger.info("Discovered %d commands from %s", len(commands), base_url)
                    return
            except Exception as e:
                logger.debug("Waiting for app: %s", e)
            await asyncio.sleep(3)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        result = []
        for cmd in commands:
            properties = {}
            required = []
            for p in cmd.get("params", []):
                properties[p["name"]] = {
                    "type": p.get("json_type", "string"),
                    "description": p.get("description", ""),
                }
                if p.get("required", True):
                    required.append(p["name"])
            if cmd.get("scope") == "session":
                properties["session_id"] = {
                    "type": "string",
                    "description": "Session ID (use list_sessions to find, or 'current' for most recent)",
                }
                required.append("session_id")
            result.append(Tool(
                name=cmd["name"],
                description=cmd.get("description", ""),
                inputSchema={"type": "object", "properties": properties, "required": required},
            ))
        return result

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        cmd = next((c for c in commands if c["name"] == name), None)
        if not cmd:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown: {name}"}))]

        session_id = arguments.pop("session_id", None)

        # Resolve "current"
        if session_id == "current":
            data = await _http_request("GET", f"{base_url}/sessions", api_key)
            sessions = data.get("sessions", [])
            if sessions:
                session_id = sessions[0]["session_id"]
            else:
                return [TextContent(type="text", text=json.dumps({"error": "No active sessions"}))]

        # Build URL
        if cmd.get("scope") == "session":
            path = cmd.get("http_path") or f"/sessions/{session_id}/{cmd['name']}"
        else:
            path = cmd.get("http_path") or f"/{cmd['name']}"

        url = f"{base_url}{path}"
        method = cmd.get("http_method", "GET")

        if method in ("POST", "PUT", "DELETE"):
            result = await _http_request(method, url, api_key, body=arguments)
        else:
            result = await _http_request(method, url, api_key, params=arguments)

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    async with stdio_server() as (read_stream, write_stream):
        # Start command discovery in background
        asyncio.create_task(discover_commands())
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
