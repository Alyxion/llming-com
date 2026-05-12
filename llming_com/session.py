"""Base session entry and registry for llming applications.

Provides:
- ``BaseSessionEntry`` — dataclass with common fields (user, websocket, timestamps)
- ``BaseSessionRegistry`` — generic singleton registry with cleanup loop

Subclass both in your application to add domain-specific fields and behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Generic, Optional, TypeVar

from llming_com.auth import AuthManager, get_auth

logger = logging.getLogger(__name__)

# Cleanup defaults
DEFAULT_CLEANUP_INTERVAL = 60  # seconds
DEFAULT_SESSION_TTL = 300  # seconds
DEFAULT_HEARTBEAT_TTL = 120  # seconds — reap "connected" sessions with no heartbeat

E = TypeVar("E", bound="BaseSessionEntry")
T = TypeVar("T")
D = TypeVar("D", bound="LlmingSessionData")

# Holds references to fire-and-forget cleanup tasks so the GC can't cancel them
# mid-run; entries are discarded automatically when the task finishes.
_pending_cleanup_tasks: set[asyncio.Task] = set()
_current_request: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "llming_current_request", default=None,
)
_current_session: contextvars.ContextVar["LlmingSession | None"] = contextvars.ContextVar(
    "llming_current_session", default=None,
)


@dataclass
class LlmingSessionData:
    """Base class for typed data attached to a ``LlmingSession``.

    Subclasses own their factory by overriding ``create``. Application code
    should access them via ``await MyData.current()``.
    """

    # Optional connection slots so subclasses can act as a ``connection_target``
    # for ``run_websocket_session`` (transport writes the websocket/controller here).
    websocket: Optional[Any] = field(default=None, init=False)
    controller: Optional[Any] = field(default=None, init=False)

    @classmethod
    async def create(cls, session: "LlmingSession", context: Any = None):
        """Create the attachment for a session.

        Subclasses override this when they can be lazily constructed. Data
        that is created from route-specific context can read that context here.
        """
        return cls()

    async def cleanup(self) -> None:
        """Optional cleanup hook called when the owning session is removed."""
        return None

    @classmethod
    async def current(
        cls: type[D],
        *,
        request: Any | None = None,
        session: "LlmingSession | None" = None,
        context: Any | None = None,
    ) -> D | None:
        """Return this attachment for the current session, creating it if needed.

        The current session can come from an explicitly active session context,
        from the current request context, or from the ``request`` / ``session``
        arguments. ``None`` means no authenticated session is available.
        """
        return await LlmingSessions.ensure(
            cls,
            request=request,
            session=session,
            context=context,
        )


@dataclass(frozen=True)
class LlmingPrincipal:
    """Authenticated user identity shared by all app sessions."""

    identity_id: str = ""
    user_id: str = ""
    user_name: str = ""
    user_email: str = ""
    user_avatar: str = ""


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
    principal: LlmingPrincipal = field(default_factory=LlmingPrincipal)
    identity_id: str = ""
    connection_id: str = ""
    authenticated_via: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.monotonic)
    _cleanup_done: bool = field(default=False, repr=False)
    _timers: dict[str, asyncio.Task] = field(default_factory=dict, init=False, repr=False)
    _attachments: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _owner_registry: Optional["BaseSessionRegistry"] = field(default=None, init=False, repr=False)

    async def send(self, msg: dict[str, Any]) -> bool:
        """Send a reactive event over this session's active controller."""
        if self.controller is None:
            return False
        return await self.controller.send(msg)

    async def call(self, target: str, *args: Any, **kwargs: Any) -> bool:
        """Call a named method on the connected browser session.

        This mirrors client-side method calls used by UI frameworks: Python
        names an addressed method such as ``"drawer.open"`` and passes
        JSON-serializable arguments; the browser dispatcher looks up that
        method on the mounted component and invokes it directly. No
        JavaScript ``eval`` is involved.
        """
        if "." not in target:
            raise ValueError("client call target must be '<component>.<method>'")
        component, method = target.rsplit(".", 1)
        return await self.send(
            {
                "type": "llming.call",
                "target": component,
                "method": method,
                "args": list(args),
                "kwargs": kwargs,
            }
        )

    def start_timer(
        self,
        key: str,
        interval: float,
        callback: Callable[[], Awaitable[bool | None]],
    ) -> asyncio.Task:
        """Run *callback* every *interval* seconds until it returns ``False``.

        The task is stored by ``key`` and an existing task with the same key
        is cancelled first. This gives applications
        per-session timers without hand-written sleep loops in handlers.
        """
        self.cancel_timer(key)

        async def runner() -> None:
            while True:
                keep_going = await callback()
                if keep_going is False:
                    return
                await asyncio.sleep(interval)

        task = asyncio.create_task(runner())
        self._timers[key] = task
        return task

    def cancel_timer(self, key: str) -> None:
        """Cancel a task previously created with :meth:`start_timer`."""
        task = self._timers.get(key)
        if task and not task.done():
            task.cancel()

    def cleanup_runtime(self) -> None:
        """Cancel timers and drop runtime attachments."""
        for task in self._timers.values():
            if not task.done():
                task.cancel()
        self._timers.clear()
        attachments = list(self._attachments.values())
        self._attachments.clear()
        for attachment in attachments:
            if isinstance(attachment, LlmingSessionData):
                try:
                    task = asyncio.get_running_loop().create_task(attachment.cleanup())
                except RuntimeError:
                    continue
                _pending_cleanup_tasks.add(task)
                task.add_done_callback(_pending_cleanup_tasks.discard)


@dataclass
class LlmingSession(BaseSessionEntry):
    """The single concrete session type stored in the central registry."""

    def active(self):
        """Bind this session as current for the surrounding async work."""
        return LlmingSessions.active(self)

    def _class_key(self, data_type: type) -> str:
        return f"{data_type.__module__}.{data_type.__qualname__}"

    def optional_data(self, data_type: type[T]) -> T | None:
        """Return a typed attachment, or None when absent."""
        value = self._attachments.get(self._class_key(data_type))
        if value is None:
            return None
        if not isinstance(value, data_type):
            raise TypeError(f"Session attachment {data_type!r} contains an invalid value")
        return value

    def attach_data(self, value: T) -> T:
        """Attach an already-created typed data object."""
        self._attachments[self._class_key(type(value))] = value
        return value

    async def ensure_data(
        self,
        data_type: type[T],
        context: Any = None,
    ) -> T:
        """Return typed session data, creating it once via its class factory."""
        existing = self.optional_data(data_type)
        if existing is not None:
            return existing
        if not issubclass(data_type, LlmingSessionData):
            raise TypeError("Session data classes must inherit LlmingSessionData")
        created = data_type.create(self, context)
        if inspect.isawaitable(created):
            created = await created
        if not isinstance(created, data_type):
            raise TypeError(f"{data_type!r}.create returned {type(created)!r}")
        return self.attach_data(created)


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
    _global_sessions: Dict[str, BaseSessionEntry] = {}
    _identity_sessions: dict[str, str] = {}
    _global_cleanup_task: Optional[asyncio.Task] = None

    def __init__(self) -> None:
        self._sessions: Dict[str, E] = BaseSessionRegistry._global_sessions  # type: ignore[assignment]

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
        BaseSessionRegistry._global_sessions.clear()
        BaseSessionRegistry._identity_sessions.clear()
        task = BaseSessionRegistry._global_cleanup_task
        if task and not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass
        BaseSessionRegistry._global_cleanup_task = None

    def register(self, session_id: str, entry: E) -> E:
        """Register a session entry."""
        entry._owner_registry = self
        if entry.identity_id:
            BaseSessionRegistry._identity_sessions[entry.identity_id] = session_id
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

    def get_identity_session(self, identity_id: str) -> Optional[E]:
        """Return the live session bound to an identity cookie, if present."""
        session_id = BaseSessionRegistry._identity_sessions.get(identity_id)
        if not session_id:
            return None
        return self.get_session(session_id)

    def register_identity_session(self, identity_id: str, entry: E) -> E:
        """Register or replace the durable HTTP session for an identity."""
        session_id = f"identity:{identity_id}"
        entry.identity_id = identity_id
        entry.authenticated_via = entry.authenticated_via or "identity_cookie"
        return self.register(session_id, entry)

    def remove(self, session_id: str) -> Optional[E]:
        """Remove and return a session entry."""
        entry = self._sessions.pop(session_id, None)
        if entry:
            if entry.identity_id:
                mapped = BaseSessionRegistry._identity_sessions.get(entry.identity_id)
                if mapped == session_id:
                    BaseSessionRegistry._identity_sessions.pop(entry.identity_id, None)
            entry.cleanup_runtime()
            logger.info("[SESSION] Removed %s", session_id[:8])
        return entry

    def list_sessions(self) -> Dict[str, E]:
        """Return a snapshot of all sessions."""
        return dict(self._sessions)

    @property
    def active_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def cleanup_expired(
        self,
        ttl: float = DEFAULT_SESSION_TTL,
        heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
    ) -> int:
        """Remove sessions idle for more than *ttl* seconds.

        Also reaps zombie sessions — sessions with a WebSocket reference
        but no heartbeat for more than *heartbeat_ttl* seconds.  These
        occur when a proxy silently drops the TCP connection and the
        server never receives a disconnect event.

        Override ``on_session_expired`` for cleanup hooks.
        """
        now = time.monotonic()
        expired = []
        for sid, entry in self._sessions.items():
            if entry.websocket is None and now - entry.last_activity > ttl:
                # Disconnected and idle — normal expiry
                expired.append(sid)
            elif entry.websocket is not None and now - entry.last_heartbeat > heartbeat_ttl:
                # Zombie — websocket ref exists but no heartbeat
                logger.warning(
                    "[SESSION] Reaping zombie session %s (no heartbeat for %ds)",
                    sid[:8],
                    int(now - entry.last_heartbeat),
                )
                try:
                    import asyncio
                    asyncio.get_running_loop().create_task(self._close_zombie(entry))
                except RuntimeError:
                    pass
                entry.websocket = None
                expired.append(sid)

        for sid in expired:
            entry = self._sessions.pop(sid)
            if entry.identity_id:
                mapped = BaseSessionRegistry._identity_sessions.get(entry.identity_id)
                if mapped == sid:
                    BaseSessionRegistry._identity_sessions.pop(entry.identity_id, None)
            entry.cleanup_runtime()
            owner = entry._owner_registry
            if owner is not None:
                owner.on_session_expired(sid, entry)
            else:
                self.on_session_expired(sid, entry)
        if expired:
            logger.info("[SESSION] Cleaned up %d expired sessions", len(expired))
        return len(expired)

    @staticmethod
    async def _close_zombie(entry: E) -> None:
        """Try to close a zombie WebSocket and run controller cleanup."""
        if entry.websocket:
            try:
                await entry.websocket.close(code=4004, reason="Heartbeat timeout")
            except Exception:
                pass
        if entry.controller:
            try:
                await entry.controller.cleanup()
            except Exception:
                pass

    def on_session_expired(self, session_id: str, entry: E) -> None:
        """Hook called when a session expires. Override for cleanup."""
        pass

    def start_cleanup_loop(
        self, interval: float = DEFAULT_CLEANUP_INTERVAL
    ) -> None:
        """Start the background cleanup task (idempotent).

        Safe to call outside an async context — silently skips if no event loop.
        """
        if BaseSessionRegistry._global_cleanup_task and not BaseSessionRegistry._global_cleanup_task.done():
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
            BaseSessionRegistry._global_cleanup_task = loop.create_task(_loop())
        except RuntimeError:
            # No running event loop — skip (cleanup will start on next call
            # from an async context, or can be triggered manually)
            pass


class LlmingSessions:
    """Central access point for process-wide session operations."""

    @classmethod
    def registry(cls) -> BaseSessionRegistry[LlmingSession]:
        """Return the single process-wide session registry."""
        return BaseSessionRegistry.get()

    @classmethod
    def create_identity_entry(cls, identity_id: str) -> LlmingSession:
        """Default identity-session factory used by request helpers."""
        return LlmingSession(
            user_id=identity_id,
            identity_id=identity_id,
            principal=LlmingPrincipal(identity_id=identity_id, user_id=identity_id),
            authenticated_via="identity_cookie",
        )

    @classmethod
    @contextlib.contextmanager
    def bind(
        cls,
        *,
        request: Any | None = None,
        session: LlmingSession | None = None,
    ):
        """Temporarily bind request/session to the current context."""
        request_token = _current_request.set(request) if request is not None else None
        session_token = _current_session.set(session) if session is not None else None
        try:
            yield
        finally:
            if session_token is not None:
                _current_session.reset(session_token)
            if request_token is not None:
                _current_request.reset(request_token)

    @classmethod
    @contextlib.contextmanager
    def active(cls, session: LlmingSession):
        """Temporarily bind ``session`` as current for this execution context."""
        with cls.bind(session=session):
            yield

    @classmethod
    async def current(
        cls,
        request: Any | None = None,
        *,
        auth: AuthManager | None = None,
        registry: BaseSessionRegistry[LlmingSession] | None = None,
        identity_factory: Callable[
            [str], LlmingSession | Awaitable[LlmingSession]
        ] | None = None,
    ) -> LlmingSession | None:
        """Resolve the current session from a request.

        The live auth cookie is preferred. If it is absent or stale, a valid
        identity cookie resolves to a durable identity-bound session so HTTP
        endpoints can still access user-scoped providers such as MS Graph.
        """
        bound_session = _current_session.get()
        if bound_session is not None:
            return bound_session

        resolved_request = request if request is not None else _current_request.get()
        if resolved_request is None:
            return None

        auth_manager = auth or get_auth()
        session_registry = registry or cls.registry()
        factory = identity_factory or cls.create_identity_entry

        session_id = auth_manager.get_auth_session_id(resolved_request)
        if session_id:
            entry = session_registry.get_session(session_id)
            if entry is not None:
                return entry

        identity_id = auth_manager.verify_identity_cookie(resolved_request)
        if not identity_id:
            return None

        entry = session_registry.get_identity_session(identity_id)
        if entry is not None:
            return entry

        created = factory(identity_id)
        if inspect.isawaitable(created):
            created = await created
        return session_registry.register_identity_session(identity_id, created)

    @classmethod
    async def ensure(
        cls,
        data_type: type[T],
        *,
        request: Any | None = None,
        session: LlmingSession | None = None,
        context: Any | None = None,
    ) -> T | None:
        """Resolve typed data attached to the current session.

        Pass a ``LlmingSessionData`` subclass whose class owns the factory::

            hub = await LlmingSessions.ensure(LlmingHubSession)

        Most application code should prefer the attachment-level shorthand::

            hub = await LlmingHubSession.current()
        """
        target_session = session or await cls.current(request)
        if target_session is None:
            return None

        creation_context = context if context is not None else request
        return await target_session.ensure_data(data_type, creation_context)
