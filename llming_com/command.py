"""Declarative command framework for llming applications.

Define commands once with ``@command`` — llming-com auto-generates
REST endpoints and MCP tools from the same definitions.

Example::

    @command("send_message", description="Send a chat message",
             scope=CommandScope.SESSION, http_method="POST")
    async def send_message(controller, text: str, images: list[str] | None = None):
        task = asyncio.create_task(controller.send_message(text, images=images))
        return {"status": "sent", "text": text}

Special parameter names are injected automatically (not user-facing):
  ``session_id``, ``entry``, ``controller``, ``registry``, ``request``, ``nudge_store``
"""

from __future__ import annotations

import enum
import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, get_type_hints


class CommandScope(enum.Enum):
    """Where a command applies."""
    GLOBAL = "global"
    SESSION = "session"


# Parameter names injected by the framework — not exposed to users
INJECTED_PARAMS = frozenset({
    "session_id", "entry", "controller", "registry", "request", "nudge_store",
})


@dataclass
class CommandParam:
    """A single user-facing parameter of a command."""
    name: str
    type: type
    description: str = ""
    required: bool = True
    default: Any = None


@dataclass
class CommandDef:
    """Complete definition of a command."""
    name: str
    description: str
    scope: CommandScope
    handler: Callable
    params: List[CommandParam] = field(default_factory=list)
    http_method: str = "GET"
    http_path: str = ""
    tags: List[str] = field(default_factory=list)
    requires_websocket: bool = False
    app: str = ""  # App type filter (e.g. "lodge", "hub"). Empty = universal.

    def input_schema(self) -> dict:
        """Generate JSON Schema for MCP tool inputSchema."""
        properties = {}
        required = []
        for p in self.params:
            properties[p.name] = {
                "type": _python_type_to_json_type(p.type),
                "description": p.description,
            }
            if p.required:
                required.append(p.name)
        if self.scope == CommandScope.SESSION:
            properties["session_id"] = {
                "type": "string",
                "description": "Session ID (use list_sessions to find, or 'current' for most recent)",
            }
            required.append("session_id")
        return {"type": "object", "properties": properties, "required": required}

    def to_dict(self) -> dict:
        """Serialize for the /commands meta-endpoint."""
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope.value,
            "http_method": self.http_method,
            "http_path": self.http_path,
            "tags": self.tags,
            "app": self.app,
            "requires_websocket": self.requires_websocket,
            "params": [
                {
                    "name": p.name,
                    "json_type": _python_type_to_json_type(p.type),
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                }
                for p in self.params
            ],
        }


class CommandRegistry:
    """Central registry for all commands in the application."""

    def __init__(self) -> None:
        self._commands: Dict[str, CommandDef] = {}

    def register(self, cmd: CommandDef) -> None:
        self._commands[cmd.name] = cmd

    def get(self, name: str) -> Optional[CommandDef]:
        return self._commands.get(name)

    def list_commands(self, app_filter: str = "") -> List[CommandDef]:
        """List commands, optionally filtered by app type.

        When ``app_filter`` is set, returns only commands that are
        universal (``app=""``) or match the filter. When empty,
        returns all commands.
        """
        if not app_filter:
            return list(self._commands.values())
        return [c for c in self._commands.values()
                if not c.app or c.app == app_filter]

    def by_scope(self, scope: CommandScope) -> List[CommandDef]:
        return [c for c in self._commands.values() if c.scope == scope]


class CommandError(Exception):
    """Raised by command handlers to signal errors."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Module-level default registry ────────────────────────────────────

_default_registry = CommandRegistry()


def get_default_command_registry() -> CommandRegistry:
    return _default_registry


# ── Decorator ────────────────────────────────────────────────────────

def command(
    name: str,
    *,
    description: str = "",
    scope: CommandScope = CommandScope.SESSION,
    http_method: str = "POST",
    http_path: str = "",
    tags: Optional[List[str]] = None,
    requires_websocket: bool = False,
    app: str = "",
    registry: Optional[CommandRegistry] = None,
) -> Callable:
    """Register an async function as a command.

    The function signature is inspected to extract user-facing parameters.
    Parameters named in ``INJECTED_PARAMS`` are provided by the framework.
    """
    def decorator(fn: Callable) -> Callable:
        try:
            hints = get_type_hints(fn)
        except Exception:
            hints = {}
        sig = inspect.signature(fn)

        params = []
        for pname, param in sig.parameters.items():
            if pname in INJECTED_PARAMS:
                continue
            ptype = hints.get(pname, str)
            # Unwrap Optional
            origin = getattr(ptype, "__origin__", None)
            if origin is typing.Union:
                args = [a for a in ptype.__args__ if a is not type(None)]
                if len(args) == 1:
                    ptype = args[0]
            is_required = param.default is inspect.Parameter.empty
            default = None if is_required else param.default
            params.append(CommandParam(
                name=pname,
                type=ptype,
                required=is_required,
                default=default,
            ))

        cmd = CommandDef(
            name=name,
            description=description or fn.__doc__ or "",
            scope=scope,
            handler=fn,
            params=params,
            http_method=http_method,
            http_path=http_path,
            tags=tags or [],
            requires_websocket=requires_websocket,
            app=app,
        )
        target = registry or _default_registry
        target.register(cmd)
        fn._command_def = cmd
        return fn

    return decorator


# ── Helpers ──────────────────────────────────────────────────────────

def _python_type_to_json_type(t) -> str:
    """Convert Python type to JSON Schema type string."""
    if t is str:
        return "string"
    if t is int:
        return "integer"
    if t is float:
        return "number"
    if t is bool:
        return "boolean"
    origin = getattr(t, "__origin__", None)
    if origin is list or t is list:
        return "array"
    if origin is dict or t is dict:
        return "object"
    return "string"
