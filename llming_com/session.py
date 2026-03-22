"""Base session entry and registry for llming applications.

Provides:
- ``BaseSessionEntry`` — dataclass with common fields (user, websocket, timestamps)
- ``BaseSessionRegistry`` — generic singleton registry with cleanup loop

Subclass both in your application to add domain-specific fields and behavior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

# Cleanup defaults
DEFAULT_CLEANUP_INTERVAL = 60  # seconds
DEFAULT_SESSION_TTL = 300  # seconds

E = TypeVar("E", bound="BaseSessionEntry")


@dataclass
class BaseSessionEntry:
    """Base session entry — subclass to add domain-specific fields.

    Common fields shared by chat, hub, and any future llming app.
    """

    user_id: str
    user_name: str = ""
    user_email: str = ""
    user_avatar: str = ""
    app_type: str = ""  # App identifier for command filtering (e.g. "lodge", "hub")
    websocket: Optional[Any] = None  # WebSocket from starlette
    controller: Optional[Any] = None
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    _cleanup_done: bool = field(default=False, repr=False)


class BaseSessionRegistry(Generic[E]):
    """Generic session registry with singleton access and TTL cleanup.

    Type parameter ``E`` is the session entry subclass, giving type-safe
    access to domain-specific fields.

    Usage::

        class MyRegistry(BaseSessionRegistry["MySessionEntry"]):
            pass

        registry = MyRegistry.get()
        entry = MyEntry(user_id="u1")
        registry.register("session-123", entry)
    """

    _instance: Optional[BaseSessionRegistry] = None

    def __init__(self) -> None:
        self._sessions: Dict[str, E] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    @classmethod
    def get(cls) -> "BaseSessionRegistry[E]":
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing)."""
        cls._instance = None

    def register(self, session_id: str, entry: E) -> E:
        """Register a session entry."""
        self._sessions[session_id] = entry
        self.start_cleanup_loop()
        logger.info("[SESSION] Registered %s for user %s", session_id[:8], entry.user_id)
        return entry

    def get_session(self, session_id: str) -> Optional[E]:
        """Get a session entry, updating last_activity."""
        entry = self._sessions.get(session_id)
        if entry:
            entry.last_activity = time.monotonic()
        return entry

    def remove(self, session_id: str) -> Optional[E]:
        """Remove and return a session entry."""
        entry = self._sessions.pop(session_id, None)
        if entry:
            logger.info("[SESSION] Removed %s", session_id[:8])
        return entry

    def list_sessions(self) -> Dict[str, E]:
        """Return a snapshot of all sessions."""
        return dict(self._sessions)

    @property
    def active_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def cleanup_expired(self, ttl: float = DEFAULT_SESSION_TTL) -> int:
        """Remove sessions idle for more than *ttl* seconds.

        Sessions with an active WebSocket connection are never expired.
        Override ``on_session_expired`` for cleanup hooks.
        """
        now = time.monotonic()
        expired = [
            sid
            for sid, entry in self._sessions.items()
            if now - entry.last_activity > ttl and entry.websocket is None
        ]
        for sid in expired:
            entry = self._sessions.pop(sid)
            self.on_session_expired(sid, entry)
        if expired:
            logger.info("[SESSION] Cleaned up %d expired sessions", len(expired))
        return len(expired)

    def on_session_expired(self, session_id: str, entry: E) -> None:
        """Hook called when a session expires. Override for cleanup."""
        pass

    def start_cleanup_loop(
        self, interval: float = DEFAULT_CLEANUP_INTERVAL
    ) -> None:
        """Start the background cleanup task (idempotent).

        Safe to call outside an async context — silently skips if no event loop.
        """
        if self._cleanup_task and not self._cleanup_task.done():
            return

        async def _loop():
            while True:
                await asyncio.sleep(interval)
                try:
                    self.cleanup_expired()
                except Exception as e:
                    logger.warning("[SESSION] Cleanup error: %s", e)

        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(_loop())
        except RuntimeError:
            # No running event loop — skip (cleanup will start on next call
            # from an async context, or can be triggered manually)
            pass
