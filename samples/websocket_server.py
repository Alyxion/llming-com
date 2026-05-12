"""Minimal WebSocket server example.

A complete FastAPI app with session creation, WebSocket transport,
and debug API. Run with:

    uvicorn samples.websocket_server:app --reload
"""

import json
import os
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from llming_com import (
    AuthManager,
    build_debug_router,
    LlmingSession,
    LlmingSessionData,
    LlmingSessions,
    run_websocket_session,
)

# ── Domain types ──────────────────────────────────────────────────────


@dataclass
class EchoState(LlmingSessionData):
    """Echo-specific state attached to the central session."""

    echo_count: int = 0


# ── App ───────────────────────────────────────────────────────────────

app = FastAPI(title="LLMing-Com Echo Server")
registry = LlmingSessions.registry()
os.environ.setdefault("LLMING_AUTH_SECRET", "demo")
# Per-app AuthManager so cookies don't collide with other llming-com apps
# sharing the same domain.
auth = AuthManager(app_name="echo")


@app.post("/sessions")
async def create_session(user_id: str = "anonymous"):
    """Create a new session and return the session ID."""
    session_id, token = auth.make_auth_cookie_value()
    entry = LlmingSession(user_id=user_id)
    registry.register(session_id, entry)
    return JSONResponse({
        "session_id": session_id,
        "token": token,
        "cookie_name": auth.auth_cookie_name,
    })


@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint that echoes messages back."""

    async def on_connect(entry, ws):
        await EchoState.current()
        await ws.send_text(json.dumps({
            "type": "connected",
            "user_id": entry.user_id,
        }))

    async def on_message(entry, msg):
        echo = await EchoState.current()
        if echo is None:
            return
        echo.echo_count += 1
        await entry.websocket.send_text(json.dumps({
            "type": "echo",
            "original": msg,
            "echo_count": echo.echo_count,
        }))

    async def on_disconnect(sid, entry):
        echo = await EchoState.current()
        count = echo.echo_count if echo is not None else 0
        print(f"[{sid[:8]}] Disconnected after {count} messages")

    await run_websocket_session(
        websocket, session_id, registry,
        on_connect=on_connect,
        on_message=on_message,
        on_disconnect=on_disconnect,
    )


# ── Debug API (localhost only) ────────────────────────────────────────

debug_router = build_debug_router(
    registry,
    api_key_env="ECHO_DEBUG_KEY",
    prefix="/debug",
    session_detail_hook=lambda sid, e: {
        "echo_count": (
            e.optional_data(EchoState).echo_count
            if e.optional_data(EchoState) is not None
            else 0
        ),
    },
)
app.include_router(debug_router)
