"""Integration test for RelayHost: a real aiortc offer is answered over a fake
relay, and HTTP is proxied to a local app through the resulting DataChannel."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import pytest
from fastapi import FastAPI, Request

from llming_com import RelayHost

aiortc = pytest.importorskip("aiortc")


class _FakeAdmission:
    """In-process stand-in for the relay SDP bridge."""

    def __init__(self) -> None:
        self.inbox: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.registered = False

    async def register_room(self, room: str, **kwargs: Any) -> None:
        self.registered = True

    async def sdp_inbox(self, room: str) -> list[dict[str, Any]]:
        msgs = self.inbox[:]
        self.inbox.clear()
        return msgs

    async def sdp_send(self, room: str, message: dict[str, Any]) -> dict[str, Any]:
        self.outbox.append(message)
        return {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornThread:
    def __init__(self, app: FastAPI, port: int) -> None:
        self.app, self.port, self._server, self._thread = app, port, None, None

    def start(self) -> None:
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not getattr(self._server, "started", False):
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    async def hello(request: Request) -> dict[str, str]:
        return {"msg": "hi", "via": request.headers.get("x-forwarded-via", "")}

    return app


@pytest.mark.asyncio
async def test_resolve_ice_servers_prefers_static_list() -> None:
    fake = _FakeAdmission()
    turn = [{"urls": ["turn:turn.example:3478"], "username": "u", "credential": "c"}]
    host = RelayHost("room-x", local_base="http://127.0.0.1:1", admission=fake, ice_servers=turn)
    assert await host._resolve_ice_servers() == turn


def test_fetch_ice_sync_parses_ice_servers_list() -> None:
    # The endpoint contract: {"ice_servers": [ {urls, username?, credential?}, ... ]}
    payload = {"ice_servers": [{"urls": ["turn:t:3478"], "username": "x", "credential": "y"}]}

    class _Resp:
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    import llming_com.p2p.relay_host as rh

    orig = rh.request.urlopen
    rh.request.urlopen = lambda *a, **k: _Resp()  # type: ignore[assignment]
    try:
        assert RelayHost._fetch_ice_sync("http://hub/api/ice") == payload["ice_servers"]
    finally:
        rh.request.urlopen = orig  # type: ignore[assignment]


def test_webrtc_peer_accepts_turn_ice_servers() -> None:
    from llming_com import WebRTCPeer

    # Should construct an RTCPeerConnection with TURN creds without raising.
    peer = WebRTCPeer(ice_servers=[{"urls": ["turn:turn.example:3478"], "username": "u", "credential": "c"}])
    assert peer.connection_state in ("new", "connecting")


@pytest.mark.asyncio
async def test_relay_host_answers_offer_and_proxies_http() -> None:
    port = _free_port()
    server = _UvicornThread(_app(), port)
    server.start()

    fake = _FakeAdmission()
    host = RelayHost(
        "room-1",
        local_base=f"http://127.0.0.1:{port}",
        admission=fake,
        stun_servers=[],            # localhost: host-only ICE
        sdp_poll_interval=0.05,
    )
    pc = aiortc.RTCPeerConnection()
    received: asyncio.Queue[str] = asyncio.Queue()
    try:
        await host.start()
        assert fake.registered

        channel = pc.createDataChannel("llming")

        @channel.on("message")
        def _on_message(message: str) -> None:
            received.put_nowait(message)

        await pc.setLocalDescription(await pc.createOffer())
        if pc.iceGatheringState != "complete":
            done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _ice() -> None:
                if pc.iceGatheringState == "complete":
                    done.set()

            await asyncio.wait_for(done.wait(), timeout=5)

        # hand the offer to the relay inbox; RelayHost polls, answers via outbox
        fake.inbox.append({"type": "offer", "sdp": pc.localDescription.sdp})
        deadline = asyncio.get_running_loop().time() + 8
        while not fake.outbox and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert fake.outbox, "relay host did not answer the offer"
        answer = fake.outbox[0]
        assert answer["type"] == "answer"
        await pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=answer["sdp"], type="answer"))

        deadline = asyncio.get_running_loop().time() + 8
        while channel.readyState != "open" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert channel.readyState == "open"

        channel.send(json.dumps({"id": "r1", "type": "http", "method": "GET", "path": "/hello", "headers": {}, "body": ""}))
        item = await asyncio.wait_for(received.get(), timeout=8)
        payload = json.loads(item)
        assert payload["status"] == 200
        body = json.loads(payload["body"])
        assert body["msg"] == "hi"
        assert body["via"] == "p2p"   # DataChannelProxy tags the forwarded request
    finally:
        await host.stop()
        await pc.close()
        server.stop()
