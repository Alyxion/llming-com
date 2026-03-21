"""Tests for llming_com.controller — BaseController."""

import asyncio
import json
import time

import pytest

from llming_com.controller import BaseController


class FakeWebSocket:
    """Mock WebSocket for testing."""
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, data: str):
        if self.closed:
            raise RuntimeError("WebSocket closed")
        self.sent.append(data)

    @property
    def client_state(self):
        return None  # unused in base controller


class TestBaseController:
    def test_init(self):
        c = BaseController("s1")
        assert c.session_id == "s1"
        assert c._ws is None

    def test_set_websocket(self):
        c = BaseController("s1")
        ws = FakeWebSocket()
        c.set_websocket(ws)
        assert c._ws is ws

    @pytest.mark.asyncio
    async def test_send_success(self):
        c = BaseController("s1")
        ws = FakeWebSocket()
        c.set_websocket(ws)
        result = await c.send({"type": "hello"})
        assert result is True
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0]) == {"type": "hello"}

    @pytest.mark.asyncio
    async def test_send_no_ws(self):
        c = BaseController("s1")
        result = await c.send({"type": "hello"})
        assert result is False

    @pytest.mark.asyncio
    async def test_send_broken_ws(self):
        c = BaseController("s1")
        ws = FakeWebSocket()
        ws.closed = True
        c.set_websocket(ws)
        result = await c.send({"type": "hello"})
        assert result is False  # silently fails

    @pytest.mark.asyncio
    async def test_send_unicode(self):
        c = BaseController("s1")
        ws = FakeWebSocket()
        c.set_websocket(ws)
        await c.send({"text": "日本語 🎉"})
        parsed = json.loads(ws.sent[0])
        assert parsed["text"] == "日本語 🎉"

    def test_rate_limit_allows(self):
        c = BaseController("s1", rate_limit_max=5)
        for _ in range(5):
            assert c.check_rate_limit() is True

    def test_rate_limit_blocks(self):
        c = BaseController("s1", rate_limit_max=3, rate_limit_window=60.0)
        assert c.check_rate_limit() is True
        assert c.check_rate_limit() is True
        assert c.check_rate_limit() is True
        assert c.check_rate_limit() is False  # blocked

    def test_rate_limit_window_expires(self):
        c = BaseController("s1", rate_limit_max=1, rate_limit_window=0.01)
        assert c.check_rate_limit() is True
        assert c.check_rate_limit() is False
        time.sleep(0.02)
        assert c.check_rate_limit() is True  # window expired

    @pytest.mark.asyncio
    async def test_handle_heartbeat(self):
        c = BaseController("s1")
        ws = FakeWebSocket()
        c.set_websocket(ws)
        await c.handle_message({"type": "heartbeat"})
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0])["type"] == "heartbeat_ack"

    @pytest.mark.asyncio
    async def test_handle_unknown_type(self):
        c = BaseController("s1")
        # Should not raise
        await c.handle_message({"type": "unknown_stuff"})

    @pytest.mark.asyncio
    async def test_cleanup(self):
        c = BaseController("s1")
        await c.cleanup()  # should not raise


class TestControllerSubclass:
    """Test that BaseController is properly extensible."""

    @pytest.mark.asyncio
    async def test_custom_message_handler(self):
        class MyController(BaseController):
            def __init__(self, session_id):
                super().__init__(session_id)
                self.received = []

            async def handle_message(self, msg):
                if msg.get("type") == "custom":
                    self.received.append(msg)
                else:
                    await super().handle_message(msg)

        c = MyController("s1")
        ws = FakeWebSocket()
        c.set_websocket(ws)

        await c.handle_message({"type": "custom", "data": "hello"})
        await c.handle_message({"type": "heartbeat"})

        assert len(c.received) == 1
        assert c.received[0]["data"] == "hello"
        assert len(ws.sent) == 1  # heartbeat_ack

    @pytest.mark.asyncio
    async def test_custom_cleanup(self):
        class MyController(BaseController):
            cleaned = False
            async def cleanup(self):
                self.cleaned = True
                await super().cleanup()

        c = MyController("s1")
        await c.cleanup()
        assert c.cleaned is True
