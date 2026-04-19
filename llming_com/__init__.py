"""llming-com — Reusable WebSocket session management, auth, and debug API.

Provides the communication infrastructure shared across llming applications:

- **Auth**: HMAC cookie signing/verification
- **Session**: Base session entry and registry with TTL cleanup
- **Transport**: WebSocket lifecycle management
- **Controller**: Base controller with send, heartbeat, rate limiting
- **WSRouter**: Namespaced dispatch for WebSocket JSON messages
- **Debug**: Debug API router for session inspection
- **DataStore**: Thread-safe in-memory session data store

Transport policy
----------------

All command and query traffic flows over the **WebSocket** via :class:`WSRouter`
— FastAPI-style namespaced dispatch on the ``"type"`` field of the JSON
message (e.g. ``{"type": "llmings.list"}``). One socket, one routing table.

**HTTP endpoints are reserved exclusively for large or static content that
does not fit the WS model:** file uploads, blob downloads, static asset
serving. Do not add HTTP routes for commands or queries — those belong on
the WS router.
"""

from llming_com.auth import (
    AUTH_COOKIE_NAME,
    IDENTITY_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthManager,
    get_auth,
)
from llming_com.command import (
    command,
    CommandDef,
    CommandError,
    CommandRegistry,
    CommandScope,
    get_default_command_registry,
)
from llming_com.command_router import build_command_router
from llming_com.controller import BaseController
from llming_com.data_store import SessionDataStore
from llming_com.debug import build_debug_router
from llming_com.session import BaseSessionEntry, BaseSessionRegistry
from llming_com.session_manager import ConnectionType, SessionContext, SessionManager
from llming_com.server import LlmingMiddleware, error_response, mount_client_static
from llming_com.transport import run_websocket_session
from llming_com.ws_router import WSRouter

__all__ = [
    # Auth
    "AuthManager",
    "get_auth",
    "AUTH_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "IDENTITY_COOKIE_NAME",
    # Session
    "BaseSessionEntry",
    "BaseSessionRegistry",
    # Transport
    "run_websocket_session",
    # Controller
    "BaseController",
    # WS message routing (see "Two routers" in README)
    "WSRouter",
    # Debug
    "build_debug_router",
    # Commands
    "command",
    "CommandDef",
    "CommandError",
    "CommandRegistry",
    "CommandScope",
    "get_default_command_registry",
    "build_command_router",
    # Session Manager
    "SessionManager",
    "SessionContext",
    "ConnectionType",
    # DataStore
    "SessionDataStore",
    # Client static
    "mount_client_static",
    # Middleware
    "LlmingMiddleware",
    "error_response",
]
