"""Base WebSocket controller for llming applications.

Provides ``BaseController`` with:
- Safe JSON send over WebSocket
- Rate limiting
- Heartbeat handling
- UI action dispatch (run_js, get_console, etc.)
- Pending response futures for request/response patterns
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BaseController:
    """Minimal WebSocket controller with send, heartbeat, and rate limiting.

    Subclass to add domain-specific message handling. Supports automatic
    dispatch via ``WSRouter`` for namespaced commands (e.g. "llmings.list").

    Args:
        session_id: The session this controller manages.
        rate_limit_window: Rate limit window in seconds.
        rate_limit_max: Max requests per window.
    """

    def __init__(
        self,
        session_id: str,
        *,
        rate_limit_window: float = 60.0,
        rate_limit_max: int = 30,
    ) -> None:
        self.session_id = session_id
        self._ws: Optional[Any] = None  # WebSocket
        self._rate_limit_window = rate_limit_window
        self._rate_limit_max = rate_limit_max
        self._request_timestamps: list[float] = []
        self._ws_dispatch_table: Optional[dict] = None  # cached from WSRouter
        self._pending_responses: dict[str, asyncio.Future] = {}  # action → Future

    def set_websocket(self, ws: Optional[Any]) -> None:
        """Set or clear the active WebSocket connection."""
        self._ws = ws

    async def send(self, msg: dict) -> bool:
        """Send a JSON message over the WebSocket.

        Safe — silently returns False if the connection is closed or broken.
        """
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            return False

    def check_rate_limit(self) -> bool:
        """Check whether the current request is within rate limits.

        Call at the start of message handlers that should be rate-limited.
        Returns True if the request is allowed, False if rate-limited.
        """
        now = time.monotonic()
        window = self._rate_limit_window
        self._request_timestamps = [
            t for t in self._request_timestamps if now - t < window
        ]
        if len(self._request_timestamps) >= self._rate_limit_max:
            return False
        self._request_timestamps.append(now)
        return True

    def mount_router(self, router: Any) -> None:
        """Mount a ``WSRouter`` for automatic message dispatch.

        Call once during setup. Messages matching router routes are
        handled automatically in ``handle_message`` before falling
        through to subclass overrides.
        """
        from llming_com.ws_router import WSRouter
        if isinstance(router, WSRouter):
            self._ws_dispatch_table = router.build_dispatch_table()

    async def handle_message(self, msg: dict) -> None:
        """Handle an incoming WebSocket message.

        Dispatch order:
        1. Heartbeat (built-in)
        2. WSRouter dispatch table (namespaced commands like "llmings.list")
        3. Subclass override (for legacy message types)
        """
        msg_type = msg.get("type", "")
        if msg_type == "heartbeat":
            await self.send({"type": "heartbeat_ack"})
            return

        # Resolve pending ui_action responses from the browser
        if msg_type == "ui_action_response":
            self.resolve_ui_response(msg)
            return

        # Try WSRouter dispatch
        if self._ws_dispatch_table:
            from llming_com.ws_router import WSRouter
            route = self._ws_dispatch_table.get(msg_type)
            if route:
                import inspect as _inspect
                kwargs: dict[str, Any] = {}
                sig = _inspect.signature(route.handler)
                for pname in sig.parameters:
                    if pname == "controller":
                        kwargs["controller"] = self
                    elif pname == "send":
                        kwargs["send"] = self.send
                    elif pname in msg:
                        kwargs[pname] = msg[pname]
                try:
                    result = await route.handler(**kwargs)
                    if result is not None:
                        resp = {"type": msg_type, **result}
                        # Preserve _req_id for request/response matching
                        if "_req_id" in msg:
                            resp["_req_id"] = msg["_req_id"]
                        await self.send(resp)
                except Exception as exc:
                    logger.error("WSRouter handler %s failed: %s", msg_type, exc)
                    resp = {"type": msg_type, "error": str(exc)}
                    if "_req_id" in msg:
                        resp["_req_id"] = msg["_req_id"]
                    await self.send(resp)
                return

        logger.debug("[CONTROLLER] Unhandled message type: %s", msg_type)

    # ── UI Action framework ────────────────────────────────

    async def send_ui_action(self, action: str, **kwargs: Any) -> bool:
        """Send a ui_action message to the browser.

        The browser's ``handleControlMessage`` dispatches these to
        a ``ui_action`` handler.
        """
        return await self.send({"type": "ui_action", "action": action, **kwargs})

    async def request_ui_action(self, action: str, timeout: float = 5.0, **kwargs: Any) -> dict[str, Any]:
        """Send a ui_action and wait for the browser's response.

        The browser sends back ``{type: "ui_action_response", action: "...", ...}``.
        Returns the response dict, or ``{"error": "timeout"}`` on timeout.
        """
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_responses[action] = future
        await self.send({"type": "ui_action", "action": action, **kwargs})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": "timeout", "action": action}
        finally:
            self._pending_responses.pop(action, None)

    def resolve_ui_response(self, msg: dict[str, Any]) -> bool:
        """Resolve a pending ui_action_response future. Returns True if matched."""
        action = msg.get("action", "")
        future = self._pending_responses.get(action)
        if future and not future.done():
            future.set_result(msg)
            return True
        return False

    async def run_js(self, code: str) -> bool:
        """Execute JavaScript in the connected browser."""
        return await self.send_ui_action("run_js", code=code)

    async def get_console_logs(self, level: str = "", pattern: str = "", since: int = 0) -> dict[str, Any]:
        """Read browser console logs. Requires client-side console capture."""
        return await self.request_ui_action(
            "get_console_logs", level=level, pattern=pattern, since=since,
        )

    async def eval_js(self, code: str, timeout: float = 5.0) -> dict[str, Any]:
        """Execute JS in browser and return the result."""
        return await self.request_ui_action("eval_js", code=code, timeout=timeout)

    async def cleanup(self) -> None:
        """Clean up resources when the session disconnects.

        Override in subclasses to cancel tasks, close connections, etc.
        """
        # Cancel any pending UI action futures
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()
