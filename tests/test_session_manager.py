"""Tests for SessionManager."""

import asyncio
from dataclasses import dataclass
from typing import Optional, Any

import pytest

from llming_com.auth import AuthManager
from llming_com.session import BaseSessionEntry, BaseSessionRegistry
from llming_com.session_manager import (
    ConnectionType,
    SessionContext,
    SessionManager,
    _FakeRequest,
)


# ── Test fixtures ──────────────────────────────────────────────


@dataclass
class TestEntry(BaseSessionEntry):
    custom_field: str = ""


class TestRegistry(BaseSessionRegistry["TestEntry"]):
    pass


@pytest.fixture(autouse=True)
def reset_registry():
    TestRegistry.reset()
    yield
    TestRegistry.reset()


@pytest.fixture
def auth():
    return AuthManager(secret="test-secret-key")


@pytest.fixture
def registry():
    return TestRegistry.get()


@pytest.fixture
def manager(registry, auth):
    return SessionManager(registry, auth)


# ── Creation ───────────────────────────────────────────────────


def test_create_session_basic(manager):
    entry = TestEntry(user_id="viewer")
    session_id, token = manager.create_session(entry)

    assert len(session_id) > 10
    assert "." in token  # HMAC signed token
    assert manager.active_count == 1


def test_create_session_with_context(manager):
    entry = TestEntry(user_id="guest")
    ctx = SessionContext(
        connection_type=ConnectionType.PROXY,
        user_email="guest@example.com",
        target_id="Xk9mPq2sY4vN",
        authenticated_via="password",
    )
    session_id, _ = manager.create_session(entry, context=ctx)

    stored_ctx = manager.get_context(session_id)
    assert stored_ctx is not None
    assert stored_ctx.connection_type == ConnectionType.PROXY
    assert stored_ctx.user_email == "guest@example.com"
    assert stored_ctx.target_id == "Xk9mPq2sY4vN"


def test_create_session_explicit_id(manager):
    entry = TestEntry(user_id="viewer")
    session_id, _ = manager.create_session(entry, session_id="my-custom-id")
    assert session_id == "my-custom-id"


def test_create_session_default_context(manager):
    entry = TestEntry(user_id="viewer")
    session_id, _ = manager.create_session(entry)
    ctx = manager.get_context(session_id)
    assert ctx is not None
    assert ctx.connection_type == ConnectionType.LAN


# ── Resolution ─────────────────────────────────────────────────


def test_resolve_from_cookie(manager, auth):
    entry = TestEntry(user_id="viewer")
    session_id, token = manager.create_session(entry)

    request = _FakeRequest({"llming_auth": token})
    resolved_id, resolved_entry = manager.resolve(request)

    assert resolved_id == session_id
    assert resolved_entry is entry


def test_resolve_missing_cookie(manager):
    entry = TestEntry(user_id="viewer")
    manager.create_session(entry)

    request = _FakeRequest({})
    resolved_id, resolved_entry = manager.resolve(request)

    assert resolved_id is None
    assert resolved_entry is None


def test_resolve_invalid_cookie(manager):
    entry = TestEntry(user_id="viewer")
    manager.create_session(entry)

    request = _FakeRequest({"llming_auth": "invalid.token.here"})
    resolved_id, _ = manager.resolve(request)
    assert resolved_id is None


def test_resolve_expired_session(manager, auth, registry):
    entry = TestEntry(user_id="viewer")
    session_id, token = manager.create_session(entry)
    registry.remove(session_id)

    request = _FakeRequest({"llming_auth": token})
    resolved_id, resolved_entry = manager.resolve(request)
    assert resolved_id is None
    assert resolved_entry is None


# ── Lifecycle ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_session(manager):
    entry = TestEntry(user_id="viewer")
    session_id, _ = manager.create_session(entry)
    assert manager.active_count == 1

    await manager.end_session(session_id)
    assert manager.active_count == 0
    assert manager.get_context(session_id) is None


@pytest.mark.asyncio
async def test_end_session_nonexistent(manager):
    # Should not raise
    await manager.end_session("nonexistent-id")


# ── Queries ────────────────────────────────────────────────────


def test_sessions_by_type(manager):
    lan_entry = TestEntry(user_id="local")
    proxy_entry = TestEntry(user_id="remote")
    p2p_entry = TestEntry(user_id="mobile")

    manager.create_session(lan_entry, context=SessionContext(connection_type=ConnectionType.LAN))
    manager.create_session(proxy_entry, context=SessionContext(connection_type=ConnectionType.PROXY))
    manager.create_session(p2p_entry, context=SessionContext(connection_type=ConnectionType.P2P))

    lan_sessions = manager.sessions_by_type(ConnectionType.LAN)
    proxy_sessions = manager.sessions_by_type(ConnectionType.PROXY)
    p2p_sessions = manager.sessions_by_type(ConnectionType.P2P)

    assert len(lan_sessions) == 1
    assert len(proxy_sessions) == 1
    assert len(p2p_sessions) == 1


def test_sessions_by_user(manager):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")
    e3 = TestEntry(user_id="u1")

    manager.create_session(e1, context=SessionContext(user_email="a@test.com"))
    manager.create_session(e2, context=SessionContext(user_email="b@test.com"))
    manager.create_session(e3, context=SessionContext(user_email="a@test.com"))

    a_sessions = manager.sessions_by_user("a@test.com")
    b_sessions = manager.sessions_by_user("b@test.com")

    assert len(a_sessions) == 2
    assert len(b_sessions) == 1


def test_active_contexts(manager):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")

    sid1, _ = manager.create_session(e1, context=SessionContext(connection_type=ConnectionType.LAN))
    sid2, _ = manager.create_session(e2, context=SessionContext(connection_type=ConnectionType.PROXY))

    contexts = manager.active_contexts()
    assert len(contexts) == 2
    assert contexts[sid1].connection_type == ConnectionType.LAN
    assert contexts[sid2].connection_type == ConnectionType.PROXY


# ── Revocation ─────────────────────────────────────────────────


def test_revoke_by_connection_type(manager):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")
    e3 = TestEntry(user_id="u3")

    manager.create_session(e1, context=SessionContext(connection_type=ConnectionType.SHARE))
    manager.create_session(e2, context=SessionContext(connection_type=ConnectionType.SHARE))
    manager.create_session(e3, context=SessionContext(connection_type=ConnectionType.LAN))

    revoked = manager.revoke_by_context(connection_type=ConnectionType.SHARE)
    assert len(revoked) == 2
    assert manager.active_count == 1


def test_revoke_by_user(manager):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")

    manager.create_session(e1, context=SessionContext(user_email="guest@test.com"))
    manager.create_session(e2, context=SessionContext(user_email="owner@test.com"))

    revoked = manager.revoke_by_context(user_email="guest@test.com")
    assert len(revoked) == 1
    assert manager.active_count == 1


def test_revoke_by_target(manager):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")

    manager.create_session(e1, context=SessionContext(target_id="host-A"))
    manager.create_session(e2, context=SessionContext(target_id="host-B"))

    revoked = manager.revoke_by_context(target_id="host-A")
    assert len(revoked) == 1


# ── Context cleanup ────────────────────────────────────────────


def test_cleanup_expired_contexts(manager, registry):
    e1 = TestEntry(user_id="u1")
    e2 = TestEntry(user_id="u2")

    sid1, _ = manager.create_session(e1)
    sid2, _ = manager.create_session(e2)

    # Remove one from registry (simulating expiry)
    registry.remove(sid1)

    cleaned = manager.cleanup_expired_contexts()
    assert cleaned == 1
    assert manager.get_context(sid1) is None
    assert manager.get_context(sid2) is not None


# ── Share sessions ─────────────────────────────────────────────


def test_share_session(manager):
    entry = TestEntry(user_id="guest-sarah")
    ctx = SessionContext(
        connection_type=ConnectionType.SHARE,
        user_display_name="Sarah",
        share_scope={"llming": "llming-lens", "windows": [42, 67]},
        share_expires_at=9999999999.0,
    )
    session_id, _ = manager.create_session(entry, context=ctx)

    stored = manager.get_context(session_id)
    assert stored.connection_type == ConnectionType.SHARE
    assert stored.share_scope["windows"] == [42, 67]
    assert stored.user_display_name == "Sarah"


# ── Properties ─────────────────────────────────────────────────


def test_registry_property(manager, registry):
    assert manager.registry is registry


def test_auth_property(manager, auth):
    assert manager.auth is auth
