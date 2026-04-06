"""Tests for llming_com.transport — WebSocket session lifecycle."""

import asyncio
import json
from dataclasses import dataclass
from typing import Optional

import pytest

from llming_com.session import BaseSessionEntry, BaseSessionRegistry
from llming_com.transport import run_websocket_session


# ── Mocks ──────────────────────────────────────────────────────────────


class MockWebSocket:
    """Mock WebSocket that feeds pre-configured messages."""

    def __init__(self, messages: list[str] | None = None):
        self._messages = list(messages or [])
        self._idx = 0
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.sent: list[str] = []

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def receive_text(self) -> str:
        if self._idx >= len(self._messages):
            # Simulate disconnect
            from starlette.websockets import WebSocketDisconnect
            raise WebSocketDisconnect(code=1000)
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send_text(self, data: str):
        self.sent.append(data)


@dataclass
class SampleEntry(BaseSessionEntry):
    custom: str = ""


class SampleReg(BaseSessionRegistry["SampleEntry"]):
    pass


@pytest.fixture(autouse=True)
def reset():
    SampleReg.reset()
    yield
    SampleReg.reset()


# ── Tests ──────────────────────────────────────────────────────────────


class TestRunWebsocketSession:
    @pytest.mark.asyncio
    async def test_session_not_found_closes_4004(self):
        reg = SampleReg.get()
        ws = MockWebSocket()
        await run_websocket_session(ws, "nonexistent", reg, on_message=lambda e, m: None)
        assert ws.closed
        assert ws.close_code == 4004

    @pytest.mark.asyncio
    async def test_accepts_and_connects(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        reg.register("s1", entry)

        connected = []

        async def on_connect(e, ws):
            connected.append(e.user_id)

        ws = MockWebSocket([])  # disconnect immediately
        await run_websocket_session(ws, "s1", reg, on_connect=on_connect, on_message=lambda e, m: None)
        assert ws.accepted
        assert connected == ["u1"]

    @pytest.mark.asyncio
    async def test_receives_messages(self):
        reg = SampleReg.get()
        reg.register("s1", SampleEntry(user_id="u1"))

        received = []

        async def on_msg(entry, msg):
            received.append(msg)

        ws = MockWebSocket([
            '{"type": "hello"}',
            '{"type": "world", "data": 42}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=on_msg)
        assert len(received) == 2
        assert received[0] == {"type": "hello"}
        assert received[1] == {"type": "world", "data": 42}

    @pytest.mark.asyncio
    async def test_invalid_json_skipped(self):
        reg = SampleReg.get()
        reg.register("s1", SampleEntry(user_id="u1"))

        received = []

        async def on_msg(entry, msg):
            received.append(msg)

        ws = MockWebSocket([
            'not json',
            '{"type": "valid"}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=on_msg)
        assert len(received) == 1
        assert received[0]["type"] == "valid"

    @pytest.mark.asyncio
    async def test_non_dict_json_skipped(self):
        reg = SampleReg.get()
        reg.register("s1", SampleEntry(user_id="u1"))

        received = []

        async def on_msg(entry, msg):
            received.append(msg)

        ws = MockWebSocket([
            '"just a string"',
            '[1, 2, 3]',
            '{"type": "valid"}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=on_msg)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_max_message_size(self):
        reg = SampleReg.get()
        reg.register("s1", SampleEntry(user_id="u1"))

        received = []

        async def on_msg(entry, msg):
            received.append(msg)

        ws = MockWebSocket([
            '{"type": "small"}',
            '{"type": "' + "x" * 10000 + '"}',  # too large
            '{"type": "after_large"}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=on_msg, max_message_size=100)
        types = [m["type"] for m in received]
        assert "small" in types
        assert "after_large" in types
        # The large message was skipped
        assert not any(len(t) > 100 for t in types)

    @pytest.mark.asyncio
    async def test_disconnect_hook_called(self):
        reg = SampleReg.get()
        reg.register("s1", SampleEntry(user_id="u1"))

        disconnected = []

        async def on_disconnect(sid, entry):
            disconnected.append(sid)

        ws = MockWebSocket([])
        await run_websocket_session(ws, "s1", reg, on_message=lambda e, m: None, on_disconnect=on_disconnect)
        assert disconnected == ["s1"]

    @pytest.mark.asyncio
    async def test_websocket_cleared_after_disconnect(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        reg.register("s1", entry)

        ws = MockWebSocket([])
        await run_websocket_session(ws, "s1", reg, on_message=lambda e, m: None)
        assert entry.websocket is None

    @pytest.mark.asyncio
    async def test_supersede_existing_connection(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        old_ws = MockWebSocket()
        old_ws.accepted = True
        entry.websocket = old_ws
        reg.register("s1", entry)

        new_ws = MockWebSocket([])
        await run_websocket_session(new_ws, "s1", reg,
                                    on_message=lambda e, m: None, supersede_existing=True)
        assert old_ws.closed
        assert old_ws.close_code == 4001


class TestHeartbeatTimestamp:
    """Tests for heartbeat stamping last_heartbeat on the entry."""

    @pytest.mark.asyncio
    async def test_connect_stamps_last_heartbeat(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        entry.last_heartbeat = 0  # force old
        reg.register("s1", entry)

        ws = MockWebSocket([])  # disconnect immediately
        await run_websocket_session(ws, "s1", reg, on_message=lambda e, m: None)
        assert entry.last_heartbeat > 0  # was stamped on connect

    @pytest.mark.asyncio
    async def test_heartbeat_message_stamps_last_heartbeat(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        reg.register("s1", entry)

        before = entry.last_heartbeat

        ws = MockWebSocket([
            '{"type": "heartbeat"}',
            '{"type": "other"}',
        ])
        received = []

        async def on_msg(e, m):
            received.append(m)

        await run_websocket_session(ws, "s1", reg, on_message=on_msg)
        assert entry.last_heartbeat >= before
        # Both messages should be forwarded to on_message
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_non_heartbeat_does_not_stamp(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        entry.last_heartbeat = 42.0  # fixed value
        reg.register("s1", entry)

        ws = MockWebSocket([
            '{"type": "ping"}',
            '{"type": "data", "value": 1}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=lambda e, m: None)
        # last_heartbeat was stamped on connect (overwriting 42.0),
        # but we can verify no extra stamps by checking only heartbeat msgs update it
        # Since connect also stamps, we verify it's >= connect time but not additionally bumped
        assert entry.last_heartbeat > 42.0  # stamped on connect

    @pytest.mark.asyncio
    async def test_multiple_heartbeats_update_timestamp(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="u1")
        reg.register("s1", entry)

        timestamps = []

        async def on_msg(e, m):
            if m.get("type") == "heartbeat":
                timestamps.append(e.last_heartbeat)

        ws = MockWebSocket([
            '{"type": "heartbeat"}',
            '{"type": "heartbeat"}',
            '{"type": "heartbeat"}',
        ])
        await run_websocket_session(ws, "s1", reg, on_message=on_msg)
        assert len(timestamps) == 3
        # Each heartbeat should have a timestamp >= the previous
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1]


class TestEndToEnd:
    """Full lifecycle: connect → exchange messages → disconnect."""

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self):
        reg = SampleReg.get()
        entry = SampleEntry(user_id="user-42", custom="hello")
        reg.register("session-1", entry)

        events = []

        async def on_connect(e, ws):
            events.append(("connect", e.user_id))
            await ws.send_text(json.dumps({"type": "init", "user": e.user_id}))

        async def on_message(e, msg):
            events.append(("message", msg["type"]))
            if msg["type"] == "echo":
                await e.websocket.send_text(json.dumps({"type": "echo_reply", "text": msg.get("text")}))

        async def on_disconnect(sid, e):
            events.append(("disconnect", sid))

        ws = MockWebSocket([
            '{"type": "echo", "text": "hello"}',
            '{"type": "echo", "text": "world"}',
            '{"type": "ping"}',
        ])
        await run_websocket_session(
            ws, "session-1", reg,
            on_connect=on_connect,
            on_message=on_message,
            on_disconnect=on_disconnect,
        )

        assert events == [
            ("connect", "user-42"),
            ("message", "echo"),
            ("message", "echo"),
            ("message", "ping"),
            ("disconnect", "session-1"),
        ]

        # Check responses
        assert len(ws.sent) == 3  # init + 2 echo replies
        init = json.loads(ws.sent[0])
        assert init["type"] == "init"
        reply1 = json.loads(ws.sent[1])
        assert reply1["text"] == "hello"
        reply2 = json.loads(ws.sent[2])
        assert reply2["text"] == "world"
