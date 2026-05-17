"""Real localhost integration tests for llming remote access."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket

from llming_com.access.remote import InMemoryAccessStore, TunnelClient, create_access_app


class UvicornThread:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None
        self._server = None

    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError(f"server did not start on {self.port}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _local_app() -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    async def hello(request: Request) -> dict[str, str]:
        return {
            "message": "hello from the private network",
            "forwarded_via": request.headers.get("x-forwarded-via", ""),
            "query": request.url.query,
        }

    @app.websocket("/ws/echo")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await websocket.send_text(f"internal:{message['text']}")
            elif message.get("bytes") is not None:
                await websocket.send_bytes(b"internal:" + message["bytes"])

    return app


async def _wait_for_online(client: httpx.AsyncClient, host_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = await client.get("/api/access/hosts")
        response.raise_for_status()
        hosts = response.json()["hosts"]
        if any(host["host_id"] == host_id and host["online"] for host in hosts):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("host did not come online")


@pytest.mark.asyncio
async def test_real_local_app_http_and_websocket_through_access_tunnel() -> None:
    store = InMemoryAccessStore()
    store.create_user("admin", "Password123!", "Admin")
    hub = UvicornThread(create_access_app(store))
    private = UvicornThread(_local_app())
    hub.start()
    private.start()
    tunnel: TunnelClient | None = None
    tunnel_task: asyncio.Task[None] | None = None
    try:
        async with httpx.AsyncClient(base_url=hub.url, timeout=10.0) as browser:
            login = await browser.post(
                "/api/access/login",
                json={"username": "admin", "password": "Password123!"},
            )
            login.raise_for_status()
            host = (await browser.post("/api/access/hosts", json={"display_name": "Private App"})).json()

            tunnel = TunnelClient(
                hub.url,
                host["connection_key"],
                local_url=private.url,
                reconnect_delay=0.1,
            )
            tunnel_task = asyncio.create_task(tunnel.run())
            await _wait_for_online(browser, host["host_id"])

            http_response = await browser.get(f"/proxy/{host['host_id']}/hello?x=42")
            assert http_response.status_code == 200
            assert http_response.json() == {
                "message": "hello from the private network",
                "forwarded_via": "proxy",
                "query": "x=42",
            }

            import websockets

            cookie = browser.cookies.get("llming_access_session")
            ws_url = hub.url.replace("http://", "ws://") + f"/proxy/{host['host_id']}/ws/echo"
            async with websockets.connect(
                ws_url,
                additional_headers={"Cookie": f"llming_access_session={cookie}"},
            ) as websocket:
                await websocket.send("ping")
                assert await websocket.recv() == "internal:ping"
                await websocket.send(b"\x01\x02")
                assert await websocket.recv() == b"internal:\x01\x02"
    finally:
        if tunnel is not None:
            await tunnel.stop()
        if tunnel_task is not None:
            tunnel_task.cancel()
            try:
                await tunnel_task
            except asyncio.CancelledError:
                pass
        hub.stop()
        private.stop()
