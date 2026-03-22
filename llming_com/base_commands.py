"""Base commands provided by llming-com.

These are shared across all implementations (lodge, hub, etc.)
and provide session management fundamentals.
"""

import time

from llming_com.command import command, CommandScope


@command("list_sessions", description="List all active sessions",
         scope=CommandScope.GLOBAL, http_method="GET")
async def list_sessions(registry):
    """List all active sessions with basic info."""
    now = time.monotonic()
    sessions = []
    for sid, entry in registry.list_sessions().items():
        sessions.append({
            "session_id": sid,
            "user_id": entry.user_id,
            "user_name": entry.user_name,
            "user_email": entry.user_email,
            "ws_connected": entry.websocket is not None,
            "idle_seconds": round(now - entry.last_activity),
        })
    return {"count": len(sessions), "sessions": sessions}
