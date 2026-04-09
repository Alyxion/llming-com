"""WebSocket command router — namespaced dispatch for WS messages.

Provides ``WSRouter`` (analogous to FastAPI's ``APIRouter``) for organizing
WebSocket message handlers into namespaced groups. Routers can be nested.

Usage::

    # In hort/commands/llmings.py
    router = WSRouter(prefix="llmings")

    @router.handler("list")
    async def list_llmings(controller):
        return {"llmings": [...]}

    @router.handler("pulse")
    async def get_pulse(controller, name: str):
        return {"name": name, "data": {...}}

    # In hort/commands/config.py
    config_router = WSRouter(prefix="config")

    @config_router.handler("get")
    async def config_get(controller, section: str):
        return {"section": section, "data": {...}}

    # In hort/app.py — assemble
    root = WSRouter()
    root.include(router)         # llmings.list, llmings.pulse
    root.include(config_router)  # config.get

    # Or nest deeper:
    admin = WSRouter(prefix="admin")
    admin.include(config_router)  # admin.config.get
    root.include(admin)

Messages arrive as ``{"type": "llmings.list", ...}`` and are routed
to the matching handler. The controller is injected automatically.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Parameter names injected by the framework (not from the WS message)
_INJECTED = frozenset({"controller", "send"})

HandlerFn = Callable[..., Coroutine[Any, Any, Any]]


class WSRoute:
    """A single WS message handler."""

    __slots__ = ("name", "handler", "full_name")

    def __init__(self, name: str, handler: HandlerFn, full_name: str = "") -> None:
        self.name = name
        self.handler = handler
        self.full_name = full_name or name


class WSRouter:
    """Namespaced WebSocket message router. Nestable.

    Args:
        prefix: Dot-separated namespace prefix (e.g. "llmings", "config").
                Empty string for the root router.
    """

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self._routes: dict[str, WSRoute] = {}
        self._children: list[WSRouter] = []

    def handler(self, name: str) -> Callable[[HandlerFn], HandlerFn]:
        """Decorator to register a WS message handler.

        The full message type will be ``{prefix}.{name}`` (or just ``{name}``
        if this is the root router with no prefix).
        """
        def decorator(fn: HandlerFn) -> HandlerFn:
            full = f"{self.prefix}.{name}" if self.prefix else name
            self._routes[name] = WSRoute(name=name, handler=fn, full_name=full)
            return fn
        return decorator

    def include(self, child: WSRouter) -> None:
        """Include a child router (like FastAPI's include_router)."""
        self._children.append(child)

    def build_dispatch_table(self, parent_prefix: str = "") -> dict[str, WSRoute]:
        """Flatten all routes into a single dispatch table.

        Keys are fully-qualified message types (e.g. "llmings.list").
        """
        table: dict[str, WSRoute] = {}
        my_prefix = f"{parent_prefix}.{self.prefix}" if parent_prefix and self.prefix else (parent_prefix or self.prefix)

        for route in self._routes.values():
            full_name = f"{my_prefix}.{route.name}" if my_prefix else route.name
            route.full_name = full_name
            table[full_name] = route

        for child in self._children:
            table.update(child.build_dispatch_table(my_prefix))

        return table

    async def dispatch(
        self,
        msg_type: str,
        msg: dict[str, Any],
        controller: Any,
        *,
        _table: dict[str, WSRoute] | None = None,
    ) -> bool:
        """Dispatch a WS message to the matching handler.

        Args:
            msg_type: The message type (e.g. "llmings.list").
            msg: The full message dict.
            controller: The controller instance (injected as ``controller``).

        Returns:
            True if handled, False if no matching route.
        """
        table = _table or self.build_dispatch_table()
        route = table.get(msg_type)
        if route is None:
            return False

        # Build kwargs: inject controller/send, pass remaining msg fields
        sig = inspect.signature(route.handler)
        kwargs: dict[str, Any] = {}
        for pname in sig.parameters:
            if pname == "controller":
                kwargs["controller"] = controller
            elif pname == "send":
                kwargs["send"] = controller.send
            elif pname in msg:
                kwargs[pname] = msg[pname]

        result = await route.handler(**kwargs)

        # Auto-send response if handler returns a value
        if result is not None:
            await controller.send({"type": msg_type, **result})

        return True
