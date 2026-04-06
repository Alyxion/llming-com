"""Unified session lifecycle manager.

Composes ``AuthManager`` (auth) + ``BaseSessionRegistry`` (storage) into a
single object that owns the full session lifecycle: create, authenticate,
track, query, and end sessions.

Sessions carry a ``SessionContext`` that records *how* the session was
established (LAN, proxy, P2P), who the user is, what they can access,
and which target system they're connected to.

Usage::

    from llming_com import SessionManager, SessionContext, ConnectionType

    manager = SessionManager(registry, auth)
    session_id, token = await manager.create_session(
        entry=MyEntry(user_id="viewer"),
        context=SessionContext(
            connection_type=ConnectionType.LAN,
            user_email="michael@openhort.ai",
        ),
    )
"""

from __future__ import annotations

import enum
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Generic, Optional, TypeVar

from llming_com.auth import AuthManager, get_auth
from llming_com.session import BaseSessionEntry, BaseSessionRegistry

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=BaseSessionEntry)


class ConnectionType(str, enum.Enum):
    """How the client reached the server."""

    LAN = "lan"
    PROXY = "proxy"
    P2P = "p2p"
    SHARE = "share"
    API = "api"


@dataclass
class SessionContext:
    """Immutable metadata about how a session was established.

    Attached to every session at creation time.  Consumers can query
    sessions by connection type, user, target, or permissions.
    """

    connection_type: ConnectionType = ConnectionType.LAN

    # Identity
    user_email: str = ""
    user_display_name: str = ""
    device_label: str = ""  # e.g. "Sarah's iPhone"

    # Auth method used to create this session
    authenticated_via: str = ""  # "cookie", "token", "password", "device_token", "share_link"

    # Target system (for multi-host / sub-hort routing)
    target_id: str = ""  # device_uid of the target host
    host_path: str = ""  # full hop chain (concatenated 12-char IDs)

    # Permissions (simple for now — full access vs scoped)
    permissions: frozenset[str] = field(default_factory=frozenset)  # empty = full access
    is_guest: bool = False
    is_owner: bool = False

    # Share link metadata (only for ConnectionType.SHARE)
    share_scope: dict[str, Any] = field(default_factory=dict)
    share_expires_at: float = 0.0  # monotonic timestamp, 0 = no expiry

    # Network info
    remote_ip: str = ""
    proxy_host_id: str = ""  # only for proxy sessions


class SessionManager(Generic[E]):
    """Unified session lifecycle manager.

    Composes ``AuthManager`` (auth) + ``BaseSessionRegistry`` (storage)
    into a single object.  All session creation, authentication, and
    cleanup goes through this class.
    """

    def __init__(
        self,
        registry: BaseSessionRegistry[E],
        auth: AuthManager | None = None,
    ) -> None:
        self._registry = registry
        self._auth = auth or get_auth()
        self._contexts: dict[str, SessionContext] = {}

    @property
    def registry(self) -> BaseSessionRegistry[E]:
        """The underlying session registry."""
        return self._registry

    @property
    def auth(self) -> AuthManager:
        """The auth manager used for cookie signing."""
        return self._auth

    # ── Session creation ───────────────────────────────────────

    def create_session(
        self,
        entry: E,
        *,
        context: SessionContext | None = None,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        """Create and register a session.

        Args:
            entry: The session entry (subclass of BaseSessionEntry).
            context: How the session was established.  If None, defaults
                to a LAN session with no special permissions.
            session_id: Optional explicit session ID.  If None, a random
                one is generated (24 bytes, URL-safe).

        Returns:
            ``(session_id, auth_token)`` — the token can be set as
            the ``llming_auth`` cookie value.
        """
        if session_id is None:
            session_id = secrets.token_urlsafe(24)

        if context is None:
            context = SessionContext()

        self._registry.register(session_id, entry)
        self._contexts[session_id] = context

        # Sign an auth cookie token
        auth_token = self._auth.sign_auth_token(session_id)

        logger.info(
            "[SESSION] Created %s (%s, user=%s, target=%s)",
            session_id[:8],
            context.connection_type.value,
            context.user_email or entry.user_id,
            context.target_id or "default",
        )
        return session_id, auth_token

    # ── Session resolution ─────────────────────────────────────

    def resolve(self, request: Any) -> tuple[str | None, E | None]:
        """Extract session_id from request and look up the entry.

        Checks (in order):
        1. ``llming_auth`` cookie (HMAC-signed)
        2. ``Authorization: Bearer <token>`` header
        3. ``session_id`` query parameter (signed)

        Returns ``(session_id, entry)`` or ``(None, None)``.
        """
        # 1. Cookie
        session_id = self._auth.get_auth_session_id(request)
        if session_id:
            entry = self._registry.get_session(session_id)
            if entry:
                return session_id, entry

        # 2. Authorization header
        auth_header = getattr(request, "headers", {}).get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            parts = token.split(".")
            if len(parts) == 3:
                candidate_id = parts[0]
                # Verify by reconstructing — reuse cookie verification logic
                if self._auth.verify_auth_cookie(
                    _FakeRequest({"llming_auth": token})
                ):
                    entry = self._registry.get_session(candidate_id)
                    if entry:
                        return candidate_id, entry

        # 3. Query parameter
        params = getattr(request, "query_params", {})
        token = params.get("session_id", "") or params.get("sid", "")
        if token and "." in token:
            if self._auth.verify_auth_cookie(_FakeRequest({"llming_auth": token})):
                candidate_id = token.split(".")[0]
                entry = self._registry.get_session(candidate_id)
                if entry:
                    return candidate_id, entry

        return None, None

    def get_context(self, session_id: str) -> SessionContext | None:
        """Return the SessionContext for a session, or None."""
        return self._contexts.get(session_id)

    # ── Session lifecycle ──────────────────────────────────────

    async def end_session(self, session_id: str) -> None:
        """End a session: cleanup controller, close WS, remove from registry."""
        entry = self._registry.get_session(session_id)
        if entry:
            # Controller cleanup
            if entry.controller and hasattr(entry.controller, "cleanup"):
                try:
                    await entry.controller.cleanup()
                except Exception:
                    pass
            # Close WebSocket
            if entry.websocket:
                try:
                    await entry.websocket.close(code=1000)
                except Exception:
                    pass

        self._registry.remove(session_id)
        self._contexts.pop(session_id, None)
        logger.info("[SESSION] Ended %s", session_id[:8])

    def revoke_by_context(
        self,
        *,
        connection_type: ConnectionType | None = None,
        user_email: str | None = None,
        target_id: str | None = None,
    ) -> list[str]:
        """Revoke (remove) sessions matching the given criteria.

        Returns list of revoked session IDs.
        """
        to_revoke = []
        for sid, ctx in self._contexts.items():
            if connection_type and ctx.connection_type != connection_type:
                continue
            if user_email and ctx.user_email != user_email:
                continue
            if target_id and ctx.target_id != target_id:
                continue
            to_revoke.append(sid)

        for sid in to_revoke:
            self._registry.remove(sid)
            self._contexts.pop(sid, None)

        if to_revoke:
            logger.info("[SESSION] Revoked %d sessions", len(to_revoke))
        return to_revoke

    # ── Queries ────────────────────────────────────────────────

    def sessions_by_type(self, conn_type: ConnectionType) -> dict[str, E]:
        """List sessions filtered by connection type."""
        return {
            sid: entry
            for sid, entry in self._registry.list_sessions().items()
            if self._contexts.get(sid, SessionContext()).connection_type == conn_type
        }

    def sessions_by_user(self, user_email: str) -> dict[str, E]:
        """List sessions for a given user email."""
        return {
            sid: entry
            for sid, entry in self._registry.list_sessions().items()
            if self._contexts.get(sid, SessionContext()).user_email == user_email
        }

    def active_contexts(self) -> dict[str, SessionContext]:
        """Return all active session contexts."""
        active_ids = set(self._registry.list_sessions().keys())
        return {sid: ctx for sid, ctx in self._contexts.items() if sid in active_ids}

    @property
    def active_count(self) -> int:
        """Number of active sessions."""
        return self._registry.active_count

    # ── Cleanup integration ────────────────────────────────────

    def cleanup_expired_contexts(self) -> int:
        """Remove contexts for sessions that no longer exist in the registry."""
        active_ids = set(self._registry.list_sessions().keys())
        stale = [sid for sid in self._contexts if sid not in active_ids]
        for sid in stale:
            del self._contexts[sid]
        return len(stale)


class _FakeRequest:
    """Minimal request-like object for cookie verification."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
