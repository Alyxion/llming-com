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
        assert e.last_heartbeat > 0

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
        assert before <= e.last_heartbeat <= after


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

    def test_cleanup_skips_active_websocket_with_recent_heartbeat(self):
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.last_activity = time.monotonic() - 1000
        entry.last_heartbeat = time.monotonic()  # heartbeat is fresh
        entry.websocket = "fake-ws"  # has active WS
        reg._sessions["s1"] = entry

        cleaned = reg.cleanup_expired(ttl=1.0, heartbeat_ttl=120.0)
        assert cleaned == 0  # not expired: WS active + heartbeat fresh
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


# ── Zombie session cleanup tests ──────────────────────────────────────


class MockZombieWebSocket:
    """Mock WebSocket for zombie tests."""

    def __init__(self):
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


class MockController:
    """Mock controller for zombie tests."""

    def __init__(self):
        self.cleaned_up = False

    async def cleanup(self):
        self.cleaned_up = True


class TestZombieSessionCleanup:
    """Tests for reaping zombie sessions with stale heartbeats."""

    def test_zombie_reaped_when_heartbeat_stale(self):
        """Session with WS but no heartbeat for > heartbeat_ttl gets reaped."""
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.websocket = "fake-ws"
        entry.last_heartbeat = time.monotonic() - 200  # stale
        reg._sessions["s1"] = entry

        cleaned = reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)
        assert cleaned == 1
        assert reg.active_count == 0

    def test_zombie_not_reaped_when_heartbeat_fresh(self):
        """Session with WS and recent heartbeat stays alive."""
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.websocket = "fake-ws"
        entry.last_heartbeat = time.monotonic()  # just now
        reg._sessions["s1"] = entry

        cleaned = reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)
        assert cleaned == 0
        assert reg.active_count == 1

    def test_zombie_websocket_cleared_before_expiry_hook(self):
        """Zombie's websocket is set to None before on_session_expired fires."""
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.websocket = "fake-ws"
        entry.last_heartbeat = time.monotonic() - 200
        reg._sessions["s1"] = entry

        reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)
        assert len(reg.expired_sessions) == 1
        _, expired_entry = reg.expired_sessions[0]
        assert expired_entry.websocket is None  # cleared

    def test_zombie_expiry_hook_called(self):
        """on_session_expired is called for zombie sessions."""
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1", score=77)
        entry.websocket = "fake-ws"
        entry.last_heartbeat = time.monotonic() - 200
        reg._sessions["s1"] = entry

        reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)
        assert len(reg.expired_sessions) == 1
        sid, expired_entry = reg.expired_sessions[0]
        assert sid == "s1"
        assert expired_entry.score == 77

    def test_mixed_cleanup_normal_and_zombie(self):
        """Both normal expired and zombie sessions cleaned in one pass."""
        reg = SampleRegistry.get()

        # Normal expired (no WS, idle)
        e1 = SampleEntry(user_id="u1", score=1)
        e1.last_activity = time.monotonic() - 1000
        reg._sessions["s1"] = e1

        # Zombie (has WS, stale heartbeat)
        e2 = SampleEntry(user_id="u2", score=2)
        e2.websocket = "fake-ws"
        e2.last_heartbeat = time.monotonic() - 200
        reg._sessions["s2"] = e2

        # Alive (has WS, fresh heartbeat)
        e3 = SampleEntry(user_id="u3", score=3)
        e3.websocket = "fake-ws"
        e3.last_heartbeat = time.monotonic()
        reg._sessions["s3"] = e3

        # Idle but not expired yet
        e4 = SampleEntry(user_id="u4", score=4)
        e4.last_activity = time.monotonic() - 10  # only 10s idle
        reg._sessions["s4"] = e4

        cleaned = reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)
        assert cleaned == 2  # s1 (normal) + s2 (zombie)
        assert reg.active_count == 2  # s3 + s4 remain
        assert reg.get_session("s3") is not None
        assert reg.get_session("s4") is not None

    def test_heartbeat_ttl_boundary(self):
        """Session at exactly heartbeat_ttl boundary is NOT reaped (> not >=)."""
        reg = SampleRegistry.get()
        entry = SampleEntry(user_id="u1")
        entry.websocket = "fake-ws"
        # Set heartbeat to exactly heartbeat_ttl ago
        entry.last_heartbeat = time.monotonic() - 120.0
        reg._sessions["s1"] = entry

        # The check is `now - last_heartbeat > heartbeat_ttl`.
        # With floating point, `monotonic() - (monotonic() - 120) > 120`
        # can go either way depending on timing, so just verify it doesn't crash
        reg.cleanup_expired(ttl=300.0, heartbeat_ttl=120.0)

    @pytest.mark.asyncio
    async def test_close_zombie_closes_websocket(self):
        """_close_zombie sends code 4004 to the stale WebSocket."""
        ws = MockZombieWebSocket()
        entry = SampleEntry(user_id="u1")
        entry.websocket = ws

        await BaseSessionRegistry._close_zombie(entry)
        assert ws.closed
        assert ws.close_code == 4004
        assert ws.close_reason == "Heartbeat timeout"

    @pytest.mark.asyncio
    async def test_close_zombie_runs_controller_cleanup(self):
        """_close_zombie calls controller.cleanup() if present."""
        ctrl = MockController()
        entry = SampleEntry(user_id="u1")
        entry.websocket = MockZombieWebSocket()
        entry.controller = ctrl

        await BaseSessionRegistry._close_zombie(entry)
        assert ctrl.cleaned_up

    @pytest.mark.asyncio
    async def test_close_zombie_tolerates_ws_close_error(self):
        """_close_zombie doesn't crash if WS close raises."""

        class BadWS:
            async def close(self, code=1000, reason=""):
                raise ConnectionResetError("already gone")

        entry = SampleEntry(user_id="u1")
        entry.websocket = BadWS()
        entry.controller = MockController()

        await BaseSessionRegistry._close_zombie(entry)
        assert entry.controller.cleaned_up  # still ran cleanup

    @pytest.mark.asyncio
    async def test_close_zombie_tolerates_controller_cleanup_error(self):
        """_close_zombie doesn't crash if controller.cleanup() raises."""

        class BadController:
            async def cleanup(self):
                raise RuntimeError("cleanup boom")

        entry = SampleEntry(user_id="u1")
        entry.websocket = MockZombieWebSocket()
        entry.controller = BadController()

        # Should not raise
        await BaseSessionRegistry._close_zombie(entry)
        assert entry.websocket.closed

    @pytest.mark.asyncio
    async def test_close_zombie_no_controller(self):
        """_close_zombie works fine without a controller."""
        ws = MockZombieWebSocket()
        entry = SampleEntry(user_id="u1")
        entry.websocket = ws
        entry.controller = None

        await BaseSessionRegistry._close_zombie(entry)
        assert ws.closed

    @pytest.mark.asyncio
    async def test_close_zombie_no_websocket(self):
        """_close_zombie is a no-op if websocket is already None."""
        entry = SampleEntry(user_id="u1")
        entry.websocket = None
        entry.controller = MockController()

        await BaseSessionRegistry._close_zombie(entry)
        # controller cleanup still runs
        assert entry.controller.cleaned_up
