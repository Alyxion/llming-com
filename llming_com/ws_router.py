"""Typed WebSocket routers for llming applications.

``SessionRouter`` handles per-session traffic and injects a typed session
entry. ``AppRouter`` handles application-wide traffic and injects the typed
app context. Both accept flat JSON payloads from the browser and can parse a
single Pydantic model parameter from that payload.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Coroutine, Literal, get_type_hints

logger = logging.getLogger(__name__)

try:
    from pydantic import BaseModel
except Exception:  # pragma: no cover - pydantic is a direct dependency
    BaseModel = None  # type: ignore[assignment]

HandlerFn = Callable[..., Coroutine[Any, Any, Any]]
RouterScope = Literal["session", "app"]
_SESSION_INJECTED = frozenset({"session", "entry", "controller", "send", "app"})
_APP_INJECTED = frozenset({"app", "caller", "session", "entry", "controller", "send"})


class WSRoute:
    """A single WS message handler."""

    __slots__ = ("name", "handler", "full_name", "scope")

    def __init__(
        self,
        name: str,
        handler: HandlerFn,
        full_name: str = "",
        *,
        scope: RouterScope = "session",
    ) -> None:
        self.name = name
        self.handler = handler
        self.full_name = full_name or name
        self.scope = scope


class _RouterBase:
    """Common router implementation. Use ``SessionRouter`` or ``AppRouter``."""

    scope: RouterScope = "session"
    required_context_name = ""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self._routes: dict[str, WSRoute] = {}
        self._children: list[_RouterBase] = []

    def handler(self, name: str) -> Callable[[HandlerFn], HandlerFn]:
        """Register a WS message handler."""

        def decorator(fn: HandlerFn) -> HandlerFn:
            self._validate_handler(fn)
            full = f"{self.prefix}.{name}" if self.prefix else name
            self._routes[name] = WSRoute(
                name=name,
                handler=fn,
                full_name=full,
                scope=self.scope,
            )
            return fn

        return decorator

    def include(self, child: "_RouterBase") -> None:
        """Include a child router with the same scope."""
        if child.scope != self.scope:
            raise TypeError("cannot include routers with different scopes")
        self._children.append(child)

    def build_dispatch_table(self, parent_prefix: str = "") -> dict[str, WSRoute]:
        """Flatten all routes into a dispatch table keyed by message type."""
        table: dict[str, WSRoute] = {}
        my_prefix = (
            f"{parent_prefix}.{self.prefix}"
            if parent_prefix and self.prefix
            else (parent_prefix or self.prefix)
        )

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
        """Dispatch a WS message to the matching handler."""
        table = _table or self.build_dispatch_table()
        route = table.get(msg_type)
        if route is None:
            return False

        result = await call_route(route, msg, controller)
        if result is not None:
            response = {"type": msg_type, **serialize_handler_result(result)}
            if "_req_id" in msg:
                response["_req_id"] = msg["_req_id"]
            await controller.send(response)
        return True

    def _validate_handler(self, fn: HandlerFn) -> None:
        if not self.required_context_name:
            return
        params = inspect.signature(fn).parameters
        if self.required_context_name not in params:
            raise TypeError(
                f"{type(self).__name__} handler {fn.__name__!r} must declare "
                f"{self.required_context_name!r}"
            )


class SessionRouter(_RouterBase):
    """Router for per-browser-session messages.

    Handlers should declare a typed ``session`` parameter and optional flat
    payload fields or a single Pydantic model payload.
    """

    scope: RouterScope = "session"
    required_context_name = "session"


class AppRouter(_RouterBase):
    """Router for application-wide messages.

    Handlers should declare a typed ``app`` parameter. If they need to know
    which browser invoked the route, they can also request ``caller``.
    """

    scope: RouterScope = "app"
    required_context_name = "app"


class WSRouter(SessionRouter):
    """Backward-compatible name for the original session-scoped router."""

    required_context_name = ""


def _is_pydantic_model_type(annotation: Any) -> bool:
    return (
        BaseModel is not None
        and inspect.isclass(annotation)
        and issubclass(annotation, BaseModel)
    )


def _message_payload(msg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in msg.items() if k not in {"type", "_req_id"}}


def build_handler_kwargs(
    handler: HandlerFn,
    msg: dict[str, Any],
    controller: Any,
    *,
    scope: RouterScope = "session",
) -> dict[str, Any]:
    """Build keyword arguments for a WS handler."""
    sig = inspect.signature(handler)
    try:
        type_hints = get_type_hints(handler)
    except Exception:
        type_hints = {}
    params = [
        param
        for param in sig.parameters.values()
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    injected = _APP_INJECTED if scope == "app" else _SESSION_INJECTED
    payload_params = [param for param in params if param.name not in injected]
    model_param = (
        payload_params[0]
        if len(payload_params) == 1
        and _is_pydantic_model_type(
            type_hints.get(payload_params[0].name, payload_params[0].annotation)
        )
        else None
    )
    session = getattr(controller, "entry", None)
    if session is None:
        session = getattr(controller, "session", None)
    app = getattr(controller, "app", None)

    kwargs: dict[str, Any] = {}
    for param in params:
        pname = param.name
        if pname == "controller":
            kwargs[pname] = controller
        elif pname == "send":
            kwargs[pname] = controller.send
        elif pname in {"session", "entry", "caller"}:
            kwargs[pname] = session
        elif pname == "app":
            kwargs[pname] = app
        elif model_param is not None and pname == model_param.name:
            raw = msg.get(pname)
            data = raw if isinstance(raw, dict) else _message_payload(msg)
            model_type = type_hints.get(pname, param.annotation)
            kwargs[pname] = model_type(**data)
        elif pname in msg:
            kwargs[pname] = msg[pname]
    return kwargs


async def call_route(route: WSRoute, msg: dict[str, Any], controller: Any) -> Any:
    kwargs = build_handler_kwargs(
        route.handler,
        msg,
        controller,
        scope=route.scope,
    )
    return await route.handler(**kwargs)


def serialize_handler_result(result: Any) -> dict[str, Any]:
    """Convert a handler return value into JSON object fields."""
    if BaseModel is not None and isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    raise TypeError(
        "router handlers must return a dict, a Pydantic model, or None"
    )
