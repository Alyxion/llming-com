"""WebSocket transport lifecycle for llming applications.

Provides ``run_websocket_session`` -- a generic WS endpoint handler
that manages the accept -> init -> message loop -> cleanup lifecycle.

Applications provide hooks for domain-specific behavior.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Awaitable, Callable, Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

from llming_com.session import BaseSessionEntry, BaseSessionRegistry

logger = logging.getLogger(__name__)


async def run_websocket_session(
    websocket: WebSocket,
    session_id: str,
    registry: BaseSessionRegistry,
    *,
    on_connect: Optional[Callable[[BaseSessionEntry, WebSocket], Awaitable[None]]] = None,
    on_message: Callable[[BaseSessionEntry, dict], Awaitable[None]],
    on_disconnect: Optional[Callable[[str, BaseSessionEntry], Awaitable[None]]] = None,
    supersede_existing: bool = True,
    max_message_size: int = 16 * 1024 * 1024,
    rate_limit: int = 100,
    rate_window: float = 60.0,
    log_prefix: str = "WS",
) -> None:
    """Run a WebSocket session through its full lifecycle.

    1. Look up session in registry (close 4004 if not found)
    2. Optionally close a previous WebSocket for the same session
    3. Accept the connection
    4. Call ``on_connect`` hook (e.g. send init payload)
    5. Receive loop: parse JSON, call ``on_message``
    6. On disconnect: call ``on_disconnect`` hook, clear websocket

    Args:
        websocket: The incoming WebSocket connection.
        session_id: Session identifier.
        registry: The session registry to look up the entry.
        on_connect: Called after accept with (entry, websocket).
        on_message: Called for each valid JSON message with (entry, msg_dict).
        on_disconnect: Called on disconnect with (session_id, entry).
        supersede_existing: If True, close any existing WebSocket for this
            session before accepting the new one.
        max_message_size: Maximum message size in bytes (default 16 MB).
        rate_limit: Maximum messages per rate_window (default 100).
        rate_window: Rate limit window in seconds (default 60).
        log_prefix: Prefix for log messages.
    """
    entry = registry.get_session(session_id)
    if not entry:
        await websocket.close(code=4004, reason="Session not found")
        return

    sid_short = session_id[:8]

    # Supersede existing connection
    if supersede_existing and entry.websocket is not None:
        try:
            await entry.websocket.close(code=4001, reason="Superseded by new connection")
        except Exception:
            pass
        if entry.controller:
            try:
                await entry.controller.cleanup()
            except Exception:
                pass

    await websocket.accept()
    entry.websocket = websocket
    logger.info("[%s] Connected: %s...", log_prefix, sid_short)

    # Transport-level rate limiting state
    _msg_timestamps: list[float] = []

    try:
        if on_connect:
            await on_connect(entry, websocket)

        # Message loop
        while True:
            raw = await websocket.receive_text()
            if max_message_size and len(raw) > max_message_size:
                logger.warning(
                    "[%s] Message too large (%d bytes) from %s...",
                    log_prefix, len(raw), sid_short,
                )
                continue

            # Transport-level rate limiting
            now = time.monotonic()
            _msg_timestamps[:] = [t for t in _msg_timestamps if now - t < rate_window]
            if len(_msg_timestamps) >= rate_limit:
                logger.warning(
                    "[%s] Rate limit exceeded for %s...",
                    log_prefix, sid_short,
                )
                continue
            _msg_timestamps.append(now)

            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                await on_message(entry, msg)
            except json.JSONDecodeError:
                logger.warning("[%s] Invalid JSON from %s...", log_prefix, sid_short)

    except WebSocketDisconnect:
        logger.info("[%s] Disconnected: %s...", log_prefix, sid_short)
    except Exception as e:
        logger.exception("[%s] Error in session %s...: %s", log_prefix, sid_short, e)
    finally:
        if on_disconnect:
            try:
                await on_disconnect(session_id, entry)
            except Exception as e:
                logger.warning("[%s] Disconnect hook error: %s", log_prefix, e)
        entry.websocket = None
