"""Real WebRTC/DataChannel integration test for the generic P2P proxy."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi import FastAPI, Request, WebSocket

from llming_com.p2p.proxy import DataChannelProxy

aiortc = pytest.importorskip("aiortc")


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


class DataChannelPeer:
    def __init__(self, channel) -> None:
        self.channel = channel

    async def send(self, data: bytes | str) -> None:
        self.channel.send(data)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


async def _wait_message(queue: asyncio.Queue, predicate, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        item = await asyncio.wait_for(queue.get(), timeout=deadline - asyncio.get_running_loop().time())
        if predicate(item):
            return item
    raise AssertionError("message was not received before timeout")


async def _complete_ice(peer) -> None:
    if peer.iceGatheringState == "complete":
        return
    event = asyncio.Event()

    @peer.on("icegatheringstatechange")
    def on_ice() -> None:
        if peer.iceGatheringState == "complete":
            event.set()

    await asyncio.wait_for(event.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_real_webrtc_datachannel_proxies_http_and_websocket() -> None:
    private = UvicornThread(_local_app())
    private.start()
    client_pc = aiortc.RTCPeerConnection()
    server_pc = aiortc.RTCPeerConnection()
    proxy_holder: dict[str, DataChannelProxy] = {}
    received: asyncio.Queue[bytes | str] = asyncio.Queue()
    try:
        client_channel = client_pc.createDataChannel("llming")

        @client_channel.on("message")
        def on_client_message(message: bytes | str) -> None:
            received.put_nowait(message)

        @server_pc.on("datachannel")
        def on_server_channel(channel) -> None:
            proxy = DataChannelProxy(DataChannelPeer(channel), local_base=private.url)
            proxy_holder["proxy"] = proxy

            @channel.on("message")
            async def on_server_message(message: bytes | str) -> None:
                await proxy.handle_message(message)

            @channel.on("open")
            def on_open() -> None:
                asyncio.create_task(proxy.start())

        offer = await client_pc.createOffer()
        await client_pc.setLocalDescription(offer)
        await _complete_ice(client_pc)

        await server_pc.setRemoteDescription(client_pc.localDescription)
        answer = await server_pc.createAnswer()
        await server_pc.setLocalDescription(answer)
        await _complete_ice(server_pc)

        await client_pc.setRemoteDescription(server_pc.localDescription)
        await _wait_for(lambda: client_channel.readyState == "open")
        await _wait_for(lambda: "proxy" in proxy_holder)

        client_channel.send(json.dumps({
            "id": "r1",
            "type": "http",
            "method": "GET",
            "path": "/hello?x=p2p",
            "headers": {},
            "body": "",
        }))
        http_response = await _wait_message(
            received,
            lambda item: isinstance(item, str) and json.loads(item).get("id") == "r1",
        )
        http_payload = json.loads(http_response)
        assert http_payload["status"] == 200
        assert json.loads(http_payload["body"]) == {
            "message": "hello from the private network",
            "forwarded_via": "p2p",
            "query": "x=p2p",
        }

        client_channel.send(json.dumps({"id": "w1", "type": "ws_open", "path": "/ws/echo"}))
        await _wait_message(
            received,
            lambda item: isinstance(item, str) and json.loads(item).get("type") == "ws_ready",
        )
        client_channel.send(json.dumps({"id": "w1", "type": "ws_text", "data": "hello"}))
        ws_response = await _wait_message(
            received,
            lambda item: isinstance(item, str)
            and json.loads(item).get("id") == "w1"
            and json.loads(item).get("type") == "ws_text",
        )
        assert json.loads(ws_response)["data"] == "internal:hello"
    finally:
        proxy = proxy_holder.get("proxy")
        if proxy is not None:
            await proxy.stop()
        await client_pc.close()
        await server_pc.close()
        private.stop()
