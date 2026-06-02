"""End-to-end test of the encrypted blind-relay proxy transport.

A browser simulator (ephemeral ECDH + AES-GCM, no aiortc) talks through an
in-memory *blind* relay (which only prepends/strips the session id and never
reads the body) to a real :class:`SecureRelayHost` forwarding to a live local
app.  Verifies both HTTP and WebSocket work and that the relay sees only
ciphertext.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

from llming_com import EphemeralKey, HostIdentity, SecureChannel, SecureRelayHost
from llming_com.p2p.secure_relay import KIND_APP, KIND_HELLO, SID_LEN


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/info")
    async def info(request: Request) -> dict[str, str]:
        return {"app": "secure", "via": request.headers.get("x-forwarded-via", "")}

    @app.websocket("/ws/echo")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_text()
                await ws.send_text(f"echo:{msg}")
        except WebSocketDisconnect:
            return

    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, app: FastAPI, port: int) -> None:
        self._s = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self._t = threading.Thread(target=self._s.run, daemon=True)

    def start(self) -> None:
        self._t.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not getattr(self._s, "started", False):
            time.sleep(0.02)

    def stop(self) -> None:
        self._s.should_exit = True
        self._t.join(timeout=5)


class _BrowserSim:
    """Minimal browser side: derives the session key and speaks the proxy protocol."""

    def __init__(self, host_pub_b64: str) -> None:
        self._eph = EphemeralKey.generate()
        self._chan: SecureChannel = self._eph.derive(host_pub_b64)
        self.to_relay: asyncio.Queue[bytes] = asyncio.Queue()   # browser -> relay (kind||body)
        self.inbox: list[dict] = []                              # decoded http responses
        self.ws_messages: asyncio.Queue[str] = asyncio.Queue()

    async def hello(self) -> None:
        await self.to_relay.put(KIND_HELLO + json.dumps({"epub": self._eph.public_b64}).encode())

    async def send_http(self, req_id: str, path: str) -> None:
        frame = json.dumps({"id": req_id, "type": "http", "method": "GET", "path": path, "headers": {}, "body": ""})
        await self.to_relay.put(KIND_APP + self._chan.seal(b"J" + frame.encode()))

    async def open_ws(self, ws_id: str, path: str) -> None:
        frame = json.dumps({"id": ws_id, "type": "ws_open", "path": path})
        await self.to_relay.put(KIND_APP + self._chan.seal(b"J" + frame.encode()))

    async def send_ws_text(self, ws_id: str, text: str) -> None:
        frame = json.dumps({"id": ws_id, "type": "ws_text", "data": text})
        await self.to_relay.put(KIND_APP + self._chan.seal(b"J" + frame.encode()))

    def on_relay_frame(self, frame: bytes) -> None:
        # frame = kind || body  (sid already stripped by the relay)
        kind, body = frame[:1], frame[1:]
        if kind != KIND_APP:
            return
        plain = self._chan.open(body)
        tag, payload = plain[:1], plain[1:]
        if tag != b"J":
            return
        msg = json.loads(payload)
        if msg.get("type") == "http_response":
            self.inbox.append(msg)
        elif msg.get("type") == "ws_text":
            self.ws_messages.put_nowait(msg["data"])


@pytest.mark.asyncio
async def test_secure_relay_http_and_ws_end_to_end() -> None:
    port = _free_port()
    server = _Server(_app(), port)
    server.start()

    identity = HostIdentity.generate()
    browser = _BrowserSim(identity.public_b64)
    sid = b"sess000000000001"  # 16 bytes

    # In-memory blind relay: host->relay frames carry the sid; relay strips it to
    # the browser. It NEVER inspects the body.
    async def host_send(frame: bytes) -> None:
        assert frame[:SID_LEN] == sid
        assert b"http_response" not in frame  # ciphertext only
        browser.on_relay_frame(frame[SID_LEN:])

    host = SecureRelayHost(host_send, identity, local_base=f"http://127.0.0.1:{port}")

    async def pump_browser_to_host() -> None:
        while True:
            kb = await browser.to_relay.get()
            assert b"epub" in kb or b"http" not in kb  # only the hello is plaintext
            await host.feed(sid + kb)

    pump = asyncio.create_task(pump_browser_to_host())
    try:
        await browser.hello()
        await asyncio.sleep(0.05)
        assert host.active_sessions == ["sess000000000001"]

        # --- HTTP over the encrypted relay ---
        await browser.send_http("r1", "/info")
        deadline = asyncio.get_running_loop().time() + 8
        while not browser.inbox and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert browser.inbox, "no http response received"
        resp = browser.inbox[0]
        assert resp["status"] == 200
        assert json.loads(resp["body"])["via"] == "proxy"

        # --- WebSocket over the encrypted relay ---
        await browser.open_ws("w1", "/ws/echo")
        await asyncio.sleep(0.1)
        await browser.send_ws_text("w1", "ping")
        got = await asyncio.wait_for(browser.ws_messages.get(), timeout=8)
        assert got == "echo:ping"
    finally:
        pump.cancel()
        await host.stop()
        server.stop()


@pytest.mark.asyncio
async def test_secure_relay_rejects_forged_host_key() -> None:
    """A relay/attacker that doesn't hold the host private key can't open a session."""
    identity = HostIdentity.generate()
    wrong = HostIdentity.generate()
    browser = _BrowserSim(wrong.public_b64)  # browser was given a forged host key

    sent: list[bytes] = []

    async def host_send(frame: bytes) -> None:
        sent.append(frame)

    host = SecureRelayHost(host_send, identity, local_base="http://127.0.0.1:1")
    sid = b"sess000000000002"
    await browser.hello()
    await host.feed(sid + await browser.to_relay.get())
    # Session opens (hello carries a valid pubkey), but the derived keys differ,
    # so the first sealed app frame fails to open and is dropped — no app traffic.
    await browser.send_http("r1", "/info")
    await host.feed(sid + await browser.to_relay.get())
    await asyncio.sleep(0.05)
    assert sent == []  # nothing proxied; the forged-key client can't talk
