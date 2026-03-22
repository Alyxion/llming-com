"""llming-com — Reusable WebSocket session management, auth, and debug API.

Provides the communication infrastructure shared by llming-chat, llming-hub,
and any future llming application:

- **Auth**: HMAC cookie signing/verification
- **Session**: Base session entry and registry with TTL cleanup
- **Transport**: WebSocket lifecycle management
- **Controller**: Base controller with send, heartbeat, rate limiting
- **Debug**: Debug API router for session inspection
- **DataStore**: Thread-safe in-memory session data store
"""

from llming_com.auth import (
    AUTH_COOKIE_NAME,
    IDENTITY_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    get_auth_session_id,
    make_auth_cookie_value,
    sign_auth_token,
    sign_identity_token,
    verify_auth_cookie,
    verify_identity_cookie,
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
from llming_com.transport import run_websocket_session

__all__ = [
    # Auth
    "AUTH_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "IDENTITY_COOKIE_NAME",
    "sign_auth_token",
    "verify_auth_cookie",
    "get_auth_session_id",
    "sign_identity_token",
    "verify_identity_cookie",
    "make_auth_cookie_value",
    # Session
    "BaseSessionEntry",
    "BaseSessionRegistry",
    # Transport
    "run_websocket_session",
    # Controller
    "BaseController",
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
    # DataStore
    "SessionDataStore",
]
