"""Tests for the FastAPI P2P host (HTTP signaling + WebRTC + proxy fallback)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.testclient import TestClient

from llming_com import create_p2p_host_app, mount_p2p_host

# ---- unit tests (no aiortc needed) ----


def _demo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse("<html><head></head><body>hi</body></html>")

    @app.get("/api/info")
    async def info(request: Request) -> JSONResponse:
        return JSONResponse({"forwarded_via": request.headers.get("x-forwarded-via", "direct")})

    return app


def test_config_advertises_http_signaling_and_no_stun_on_localhost() -> None:
    app = create_p2p_host_app("http://127.0.0.1:8000")
    client = TestClient(app)
    cfg = client.get("/p2p/config").json()
    assert cfg["signal"] == "http"
    assert cfg["offer_url"] == "/p2p/offer"
    assert cfg["stun_servers"] == []  # localhost → host-only ICE
    assert cfg["mode"] == "p2p"


def test_config_uses_stun_for_remote_base() -> None:
    app = create_p2p_host_app("https://host.example.com")
    cfg = TestClient(app).get("/p2p/config").json()
    assert cfg["stun_servers"] == ["stun:stun.l.google.com:19302"]


def test_proxy_mode_requires_fallback_url_else_downgrades() -> None:
    app = create_p2p_host_app("http://127.0.0.1:8000", mode="p2p+proxy")  # no proxy url
    cfg = TestClient(app).get("/p2p/config").json()
    assert cfg["mode"] == "p2p"  # downgraded


def test_proxy_fallback_url_is_surfaced() -> None:
    app = create_p2p_host_app(
        "http://127.0.0.1:8000", mode="p2p+proxy", proxy_fallback_url="https://hub/x"
    )
    cfg = TestClient(app).get("/p2p/config").json()
    assert cfg["mode"] == "p2p+proxy"
    assert cfg["proxy_fallback_url"] == "https://hub/x"


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        create_p2p_host_app("http://127.0.0.1:8000", mode="bogus")


def test_viewer_served() -> None:
    app = create_p2p_host_app("http://127.0.0.1:8000")
    body = TestClient(app).get("/p2p/viewer.html").text
    assert "RTCPeerConnection" in body
    assert "proxyFetch" in body


def test_offer_without_sdp_is_400() -> None:
    app = create_p2p_host_app("http://127.0.0.1:8000")
    resp = TestClient(app).post("/p2p/offer", json={})
    assert resp.status_code == 400


# ---- aiortc integration: browser-style offer -> answer -> HTTP over DataChannel ----

aiortc = pytest.importorskip("aiortc")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornThread:
    def __init__(self, app: FastAPI, port: int) -> None:
        self.app = app
        self.port = port
        self._server = None
        self._thread = None

    def start(self) -> None:
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)


@pytest.mark.asyncio
async def test_http_offer_endpoint_establishes_tunnel() -> None:
    import httpx

    port = _free_port()
    app = _demo_app()
    mount_p2p_host(app, local_base=f"http://127.0.0.1:{port}")
    server = _UvicornThread(app, port)
    server.start()

    pc = aiortc.RTCPeerConnection()
    received: asyncio.Queue[str] = asyncio.Queue()
    try:
        channel = pc.createDataChannel("llming")

        @channel.on("message")
        def _on_message(message: str) -> None:
            received.put_nowait(message)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        # wait for ICE gathering to complete (no trickle)
        if pc.iceGatheringState != "complete":
            done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _ice() -> None:
                if pc.iceGatheringState == "complete":
                    done.set()

            await asyncio.wait_for(done.wait(), timeout=5)

        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"http://127.0.0.1:{port}/p2p/offer",
                json={"sdp": pc.localDescription.sdp, "type": "offer"},
            )
        answer = resp.json()
        assert answer["type"] == "answer"
        await pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=answer["sdp"], type="answer"))

        # wait for the channel to open, then issue an HTTP request over it
        deadline = asyncio.get_running_loop().time() + 8
        while channel.readyState != "open" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert channel.readyState == "open"

        channel.send(json.dumps({"id": "r1", "type": "http", "method": "GET", "path": "/api/info", "headers": {}, "body": ""}))
        item = await asyncio.wait_for(received.get(), timeout=8)
        payload = json.loads(item)
        assert payload["id"] == "r1"
        assert payload["status"] == 200
        assert json.loads(payload["body"])["forwarded_via"] == "p2p"
    finally:
        await pc.close()
        server.stop()
