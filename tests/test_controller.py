"""Tests for llming_com.controller — BaseController."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from llming_com.app import BaseLlmingApp
from llming_com.controller import BaseController
from llming_com.session import BaseSessionEntry, BaseSessionRegistry
from llming_com.ws_router import AppRouter, SessionRouter, WSRouter


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


@dataclass
class ControllerSession(BaseSessionEntry):
    state: dict[str, Any] = field(default_factory=dict)


class CounterInput(BaseModel):
    by: int = 1


class CounterOutput(BaseModel):
    ok: bool
    value: int


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
    async def test_ws_router_injects_session_and_pydantic_models(self):
        router = WSRouter(prefix="counter")

        @router.handler("inc")
        async def inc(session: ControllerSession, event: CounterInput) -> CounterOutput:
            session.state["count"] = int(session.state.get("count", 0)) + event.by
            await session.send(
                {"type": "counter.update", "value": session.state["count"]}
            )
            return CounterOutput(ok=True, value=session.state["count"])

        c = BaseController("s1")
        entry = ControllerSession(user_id="u1")
        c.attach_session(entry)
        c.mount_router(router)
        ws = FakeWebSocket()
        c.set_websocket(ws)

        await c.handle_message({"type": "counter.inc", "by": 3, "_req_id": "r1"})

        assert entry.controller is c
        assert entry.state["count"] == 3
        assert [json.loads(item) for item in ws.sent] == [
            {"type": "counter.update", "value": 3},
            {"type": "counter.inc", "ok": True, "value": 3, "_req_id": "r1"},
        ]

    @pytest.mark.asyncio
    async def test_ws_router_injects_entry_alias(self):
        router = WSRouter(prefix="flags")

        @router.handler("set")
        async def set_flag(entry: ControllerSession) -> dict:
            entry.state["enabled"] = True
            return {"ok": True}

        c = BaseController("s1")
        entry = ControllerSession(user_id="u1")
        c.attach_session(entry)
        c.mount_router(router)
        ws = FakeWebSocket()
        c.set_websocket(ws)

        await c.handle_message({"type": "flags.set"})

        assert entry.state["enabled"] is True
        assert json.loads(ws.sent[0]) == {"type": "flags.set", "ok": True}

    def test_session_router_requires_session_parameter(self):
        router = SessionRouter(prefix="bad")

        with pytest.raises(TypeError):

            @router.handler("missing")
            async def missing_context() -> dict:
                return {"ok": True}

    @pytest.mark.asyncio
    async def test_app_router_injects_app_and_caller(self):
        registry: BaseSessionRegistry[ControllerSession] = BaseSessionRegistry()
        app = BaseLlmingApp(registry)
        router = AppRouter(prefix="admin")

        @router.handler("broadcast")
        async def broadcast(
            app: BaseLlmingApp[ControllerSession],
            caller: ControllerSession,
            event: CounterInput,
        ) -> CounterOutput:
            caller.state["seen"] = event.by
            return CounterOutput(ok=True, value=len(app.registry.list_sessions()))

        c = BaseController("s1")
        entry = ControllerSession(user_id="u1")
        registry.register("s1", entry)
        c.attach_session(entry)
        c.attach_app(app)
        c.mount_app_router(router)
        ws = FakeWebSocket()
        c.set_websocket(ws)

        await c.handle_message({"type": "admin.broadcast", "by": 4})

        assert entry.state["seen"] == 4
        assert json.loads(ws.sent[0]) == {
            "type": "admin.broadcast",
            "ok": True,
            "value": 1,
        }

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
