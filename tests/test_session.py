"""Tests for llming_com.session -- BaseSessionEntry and BaseSessionRegistry."""

import time
from dataclasses import dataclass

import pytest

from llming_com.session import BaseSessionEntry, BaseSessionRegistry


# ── Custom subclasses for testing ──────────────────────────────────────


@dataclass
class SampleEntry(BaseSessionEntry):
    """Test entry with extra fields."""
    custom_data: str = ""
    score: int = 0


class SampleRegistry(BaseSessionRegistry["SampleEntry"]):
    """Test registry subclass."""

    def __init__(self):
        super().__init__()
        self.expired_sessions: list = []

    def on_session_expired(self, session_id, entry):
        self.expired_sessions.append((session_id, entry))


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset singleton between tests."""
    SampleRegistry.reset()
    yield
    SampleRegistry.reset()


# ── BaseSessionEntry tests ─────────────────────────────────────────────


class TestBaseSessionEntry:
    def test_defaults(self):
        e = BaseSessionEntry(user_id="u1")
        assert e.user_id == "u1"
        assert e.user_name == ""
        assert e.user_email == ""
        assert e.user_avatar == ""
        assert e.app_type == ""
        assert e.websocket is None
        assert e.controller is None
        assert e.created_at > 0
        assert e.last_activity > 0

    def test_custom_fields(self):
        e = SampleEntry(user_id="u1", custom_data="hello", score=42)
        assert e.custom_data == "hello"
        assert e.score == 42
        assert e.user_id == "u1"

    def test_timestamps_are_recent(self):
        before = time.monotonic()
        e = BaseSessionEntry(user_id="u1")
        after = time.monotonic()
        assert before <= e.created_at <= after
        assert before <= e.last_activity <= after


# ── BaseSessionRegistry tests ──────────────────────────────────────────


class TestBaseSessionRegistry:
    def test_singleton(self):
        r1 = SampleRegistry.get()
        r2 = SampleRegistry.get()
        assert r1 is r2

    def test_reset(self):
        r1 = SampleRegistry.get()
        SampleRegistry.reset()
        r2 = SampleRegistry.get()
        assert r1 is not r2

    def test_register_and_get(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1", custom_data="test")
        reg.register("s1", entry)
        assert reg.active_count == 1

        got = reg.get_session("s1")
        assert got is entry
        assert got.custom_data == "test"

    def test_get_nonexistent(self):
        reg = SampleRegistry.get()
        assert reg.get_session("nonexistent") is None

    def test_get_updates_last_activity(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.last_activity = 0  # force old
        reg.register("s1", entry)

        before = entry.last_activity
        time.sleep(0.01)
        reg.get_session("s1")
        assert entry.last_activity > before

    def test_remove(self):
        reg = SampleRegistry.get()
        reg.register("s1", SampleEntry(user_id="u1"))
        assert reg.active_count == 1

        removed = reg.remove("s1")
        assert removed is not None
        assert removed.user_id == "u1"
        assert reg.active_count == 0

    def test_remove_nonexistent(self):
        reg = SampleRegistry.get()
        assert reg.remove("nope") is None

    def test_list_sessions(self):
        reg = SampleRegistry.get()
        reg.register("s1", SampleEntry(user_id="u1"))
        reg.register("s2", SampleEntry(user_id="u2"))
        sessions = reg.list_sessions()
        assert len(sessions) == 2
        assert "s1" in sessions
        assert "s2" in sessions

    def test_active_count(self):
        reg = SampleRegistry.get()
        assert reg.active_count == 0
        reg.register("s1", SampleEntry(user_id="u1"))
        assert reg.active_count == 1
        reg.register("s2", SampleEntry(user_id="u2"))
        assert reg.active_count == 2

    def test_cleanup_expired(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.last_activity = time.monotonic() - 1000  # very old
        reg._sessions["s1"] = entry  # bypass register to avoid touching last_activity

        cleaned = reg.cleanup_expired(ttl=1.0)
        assert cleaned == 1
        assert reg.active_count == 0

    def test_cleanup_skips_active_websocket(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.last_activity = time.monotonic() - 1000
        entry.websocket = "fake-ws"  # has active WS
        reg._sessions["s1"] = entry

        cleaned = reg.cleanup_expired(ttl=1.0)
        assert cleaned == 0  # not expired because WS is active
        assert reg.active_count == 1

    def test_cleanup_calls_hook(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1", custom_data="important")
        entry.last_activity = time.monotonic() - 1000
        reg._sessions["s1"] = entry

        reg.cleanup_expired(ttl=1.0)
        assert len(reg.expired_sessions) == 1
        assert reg.expired_sessions[0][0] == "s1"
        assert reg.expired_sessions[0][1].custom_data == "important"

    def test_multiple_sessions(self):
        reg = SampleRegistry.get()
        for i in range(10):
            reg.register(f"s{i}", SampleEntry(user_id=f"u{i}"))
        assert reg.active_count == 10

        reg.remove("s5")
        assert reg.active_count == 9
        assert reg.get_session("s5") is None
        assert reg.get_session("s3") is not None

    def test_register_overwrites(self):
        reg = SampleRegistry.get()
        e1 = SampleEntry(user_id="u1", score=1)
        e2 = SampleEntry(user_id="u1", score=2)
        reg.register("s1", e1)
        reg.register("s1", e2)
        assert reg.active_count == 1
        assert reg.get_session("s1").score == 2

    def test_list_sessions_returns_copy(self):
        reg = SampleRegistry.get()
        reg.register("s1", SampleEntry(user_id="u1"))
        sessions = reg.list_sessions()
        sessions.pop("s1")
        # Original should be unaffected
        assert reg.active_count == 1

    def test_on_session_expired_hook(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1", score=99)
        entry.last_activity = time.monotonic() - 1000
        reg._sessions["s1"] = entry

        reg.cleanup_expired(ttl=1.0)
        assert len(reg.expired_sessions) == 1
        sid, expired_entry = reg.expired_sessions[0]
        assert sid == "s1"
        assert expired_entry.score == 99
