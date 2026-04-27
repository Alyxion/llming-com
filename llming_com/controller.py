"""Base WebSocket controller for llming applications.

Provides ``BaseController`` with:
- Safe JSON send over WebSocket
- Rate limiting
- Heartbeat handling
- SessionRouter/AppRouter dispatch (mounts namespaced routers so handlers like
  ``llmings.list`` are dispatched automatically before subclass overrides)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BaseController:
    """Minimal WebSocket controller with send, heartbeat, and rate limiting.

    Subclass to add domain-specific message handling. Supports automatic
    dispatch via :class:`~llming_com.ws_router.SessionRouter` for namespaced
    commands (e.g. ``"llmings.list"``).

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
        self._ws_dispatch_table: Optional[dict] = None
        self._app_dispatch_table: Optional[dict] = None
        self.entry: Any | None = None
        self.app: Any | None = None

    @property
    def session(self) -> Any | None:
        """Alias for the session entry attached to this controller."""
        return self.entry

    @session.setter
    def session(self, value: Any | None) -> None:
        self.entry = value

    def attach_session(self, entry: Any) -> None:
        """Attach a session entry and mirror the controller on the entry."""
        self.entry = entry
        if entry is not None:
            entry.controller = self

    def attach_app(self, app: Any) -> None:
        """Attach an application context to this controller."""
        self.app = app

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
        """Mount a session or app router for auto dispatch.

        Call once during setup. Messages whose ``"type"`` matches a route
        in the router (or any nested router) are handled automatically in
        :meth:`handle_message` before the subclass override is consulted.
        """
        from llming_com.ws_router import AppRouter, SessionRouter
        if isinstance(router, AppRouter):
            self._app_dispatch_table = router.build_dispatch_table()
        elif isinstance(router, SessionRouter):
            self._ws_dispatch_table = router.build_dispatch_table()

    def mount_session_router(self, router: Any) -> None:
        """Mount a session-scoped router."""
        from llming_com.ws_router import SessionRouter
        if not isinstance(router, SessionRouter):
            raise TypeError("mount_session_router requires SessionRouter")
        self._ws_dispatch_table = router.build_dispatch_table()

    def mount_app_router(self, router: Any) -> None:
        """Mount an app-scoped router."""
        from llming_com.ws_router import AppRouter
        if not isinstance(router, AppRouter):
            raise TypeError("mount_app_router requires AppRouter")
        self._app_dispatch_table = router.build_dispatch_table()

    async def handle_message(self, msg: dict) -> None:
        """Handle an incoming WebSocket message.

        Dispatch order:
        1. Heartbeat (built-in)
        2. SessionRouter/AppRouter dispatch table (``"llmings.list"``)
        3. Subclass override (legacy / unstructured message types)
        """
        msg_type = msg.get("type", "")
        if msg_type == "heartbeat":
            await self.send({"type": "heartbeat_ack"})
            return

        for table in (self._ws_dispatch_table, self._app_dispatch_table):
            if not table:
                continue
            route = table.get(msg_type)
            if route:
                await self._handle_route(route, msg_type, msg)
                return

        logger.debug("[CONTROLLER] Unhandled message type: %s", msg_type)

    async def _handle_route(self, route: Any, msg_type: str, msg: dict) -> None:
        from llming_com.ws_router import call_route, serialize_handler_result

        try:
            result = await call_route(route, msg, self)
            if result is not None:
                resp = {"type": msg_type, **serialize_handler_result(result)}
                if "_req_id" in msg:
                    resp["_req_id"] = msg["_req_id"]
                await self.send(resp)
        except Exception as exc:
            logger.error("%s handler %s failed: %s", route.scope, msg_type, exc)
            resp = {"type": msg_type, "error": str(exc)}
            if "_req_id" in msg:
                resp["_req_id"] = msg["_req_id"]
            await self.send(resp)

    async def cleanup(self) -> None:
        """Clean up resources when the session disconnects.

        Override in subclasses to cancel tasks, close connections, etc.
        """
        pass
