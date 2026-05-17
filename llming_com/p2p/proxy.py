"""Browser P2P proxy helpers for llming applications.

The server side receives messages from a WebRTC DataChannel and forwards them
to the app's local HTTP/WebSocket interface.  It deliberately contains no
OpenHort-specific paths or UI assumptions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

WS_ID_LEN = 4
TOKEN_EXPIRY_SECONDS = 60.0
RECONNECT_TOKEN_TTL_SECONDS = 240.0


class OneTimeTokenStore:
    """Short-lived high-entropy tokens for QR/deep-link P2P entry."""

    def __init__(self, ttl: float = TOKEN_EXPIRY_SECONDS) -> None:
        self.ttl = ttl
        self._tokens: dict[str, float] = {}

    def generate(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic()
        self._cleanup()
        return token

    def verify(self, token: str, *, consume: bool = True) -> bool:
        self._cleanup()
        if token not in self._tokens:
            return False
        if consume:
            self._tokens.pop(token, None)
        return True

    def _cleanup(self) -> None:
        now = time.monotonic()
        for token, created_at in list(self._tokens.items()):
            if now - created_at > self.ttl:
                self._tokens.pop(token, None)

    @property
    def pending_count(self) -> int:
        self._cleanup()
        return len(self._tokens)


class ReconnectTokenStore:
    """Refreshable tokens that let a connected browser recover a tab reload."""

    def __init__(self, ttl: float = RECONNECT_TOKEN_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._tokens: dict[str, float] = {}

    def generate(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic()
        self._cleanup()
        return token

    def refresh(self, token: str) -> bool:
        self._cleanup()
        if token not in self._tokens:
            return False
        self._tokens[token] = time.monotonic()
        return True

    def verify(self, token: str) -> bool:
        self._cleanup()
        return token in self._tokens

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def _cleanup(self) -> None:
        now = time.monotonic()
        for token, refreshed_at in list(self._tokens.items()):
            if now - refreshed_at > self.ttl:
                self._tokens.pop(token, None)

    @property
    def active_count(self) -> int:
        self._cleanup()
        return len(self._tokens)


class DataChannelProxy:
    """Multiplex HTTP and WebSocket traffic over a WebRTC DataChannel.

    ``peer`` only needs an async ``send(bytes | str)`` method, which keeps this
    class independent from a specific WebRTC library.  aiortc, native bridges,
    and tests can all adapt to that small shape.
    """

    def __init__(
        self,
        peer: Any,
        *,
        local_base: str = "http://127.0.0.1:8765",
        ws_base: str | None = None,
        forwarded_via: str = "p2p",
    ) -> None:
        self._peer = peer
        self._local_base = local_base.rstrip("/")
        self._ws_base = (ws_base or self._local_base.replace("http://", "ws://").replace("https://", "wss://")).rstrip("/")
        self._forwarded_via = forwarded_via
        self._http_client: Any = None
        self._ws_connections: dict[str, Any] = {}
        self._video_track: Any = None
        self._reconnect_store: ReconnectTokenStore | None = None
        self._reconnect_token = ""

    async def start(self) -> None:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(base_url=self._local_base, timeout=30.0)

    async def stop(self) -> None:
        for websocket in list(self._ws_connections.values()):
            try:
                await websocket.close()
            except Exception:
                pass
        self._ws_connections.clear()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def attach_reconnect_store(self, store: ReconnectTokenStore) -> None:
        self._reconnect_store = store

    def attach_video_track(self, track: Any) -> None:
        """Attach an optional app-specific video track.

        The proxy only calls conventional ``fps``, ``set_window()``, and
        ``update_viewport()`` hooks when present.  Apps that do not stream
        video never need to use this.
        """

        self._video_track = track

    async def handle_message(self, data: bytes | str) -> None:
        if isinstance(data, bytes):
            await self._handle_ws_binary(data)
            return
        try:
            msg = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return
        msg_type = msg.get("type", "")
        if msg_type == "ping":
            await self._handle_ping(msg)
        elif msg_type == "http":
            asyncio.create_task(self._handle_http(msg))
        elif msg_type == "ws_open":
            asyncio.create_task(self._handle_ws_open(msg))
        elif msg_type == "ws_text":
            await self._handle_ws_text(msg)
        elif msg_type == "ws_close":
            await self._handle_ws_close(msg)
        elif msg_type == "video_config":
            self._handle_video_config(msg)

    def _handle_video_config(self, msg: dict[str, Any]) -> None:
        track = self._video_track
        if track is None:
            return
        try:
            if "fps" in msg and hasattr(track, "fps"):
                track.fps = int(msg["fps"])
            if "window_id" in msg and hasattr(track, "set_window"):
                track.set_window(int(msg["window_id"]))
            viewport_keys = {
                "viewport_x",
                "viewport_y",
                "viewport_w",
                "viewport_h",
                "client_width",
                "client_height",
                "zoom",
            }
            if viewport_keys & msg.keys() and hasattr(track, "update_viewport"):
                track.update_viewport(msg)
        except (TypeError, ValueError):
            return

    async def _handle_ping(self, msg: dict[str, Any]) -> None:
        pong = {"type": "pong", "ts": msg.get("ts", 0)}
        if self._reconnect_store is not None:
            if self._reconnect_token:
                self._reconnect_store.refresh(self._reconnect_token)
            else:
                self._reconnect_token = self._reconnect_store.generate()
            pong["reconnect_token"] = self._reconnect_token
        await self._peer.send(json.dumps(pong))

    async def _handle_http(self, msg: dict[str, Any]) -> None:
        if self._http_client is None:
            await self.start()
        req_id = msg.get("id", "")
        headers = dict(msg.get("headers", {}))
        headers.pop("host", None)
        headers.pop("Host", None)
        headers["x-forwarded-via"] = self._forwarded_via
        try:
            response = await self._http_client.request(
                method=msg.get("method", "GET"),
                url=msg.get("path", "/"),
                headers=headers,
                content=(msg.get("body") or "").encode("latin-1"),
            )
            content_type = response.headers.get("content-type", "")
            is_text = (
                not content_type
                or content_type.startswith("text/")
                or "json" in content_type
                or "javascript" in content_type
                or content_type.endswith("/xml")
                or content_type.endswith("/svg+xml")
            )
            body = response.text if is_text else base64.b64encode(response.content).decode("ascii")
            await self._peer.send(json.dumps({
                "id": req_id,
                "type": "http_response",
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": body,
                "binary": not is_text,
            }))
        except Exception as exc:
            await self._peer.send(json.dumps({
                "id": req_id,
                "type": "http_response",
                "status": 502,
                "headers": {},
                "body": str(exc),
                "binary": False,
            }))

    async def _handle_ws_open(self, msg: dict[str, Any]) -> None:
        import websockets

        ws_id = msg.get("id", "")
        url = self._ws_base + msg.get("path", "")
        try:
            websocket = await websockets.connect(url, additional_headers={"x-forwarded-via": self._forwarded_via})
            self._ws_connections[ws_id] = websocket
            await self._peer.send(json.dumps({"id": ws_id, "type": "ws_ready"}))
            asyncio.create_task(self._ws_read_loop(ws_id, websocket))
        except Exception as exc:
            await self._peer.send(json.dumps({"id": ws_id, "type": "ws_close", "reason": str(exc)}))

    async def _ws_read_loop(self, ws_id: str, websocket: Any) -> None:
        id_bytes = ws_id.encode("utf-8")[:WS_ID_LEN].ljust(WS_ID_LEN, b"\0")
        latest_binary: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        running = True

        async def sender() -> None:
            while running:
                try:
                    payload = await asyncio.wait_for(latest_binary.get(), timeout=1.0)
                    await self._peer.send(id_bytes + payload)
                except TimeoutError:
                    continue
                except Exception:
                    break

        send_task = asyncio.create_task(sender())
        try:
            async for payload in websocket:
                if isinstance(payload, str):
                    await self._peer.send(json.dumps({"id": ws_id, "type": "ws_text", "data": payload}))
                else:
                    if latest_binary.full():
                        try:
                            latest_binary.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    latest_binary.put_nowait(payload)
        except Exception as exc:
            logger.debug("proxied websocket %s closed: %s", ws_id, exc)
        finally:
            running = False
            send_task.cancel()
            self._ws_connections.pop(ws_id, None)
            try:
                await self._peer.send(json.dumps({"id": ws_id, "type": "ws_close"}))
            except Exception:
                pass

    async def _handle_ws_text(self, msg: dict[str, Any]) -> None:
        websocket = self._ws_connections.get(msg.get("id", ""))
        data = msg.get("data", "")
        if data and self._video_track:
            try:
                ws_msg = json.loads(data)
                if ws_msg.get("type") == "stream_config":
                    stream_msg: dict[str, Any] = {}
                    if ws_msg.get("window_id") is not None:
                        stream_msg["window_id"] = ws_msg["window_id"]
                    if ws_msg.get("fps") is not None:
                        stream_msg["fps"] = ws_msg["fps"]
                    screen_width = ws_msg.get("screen_width", 0)
                    screen_dpr = ws_msg.get("screen_dpr", 1.0)
                    if screen_width:
                        stream_msg["client_width"] = int(screen_width * screen_dpr)
                        stream_msg["client_height"] = int(screen_width * screen_dpr * 9 / 16)
                    self._handle_video_config(stream_msg)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if websocket is not None:
            try:
                await websocket.send(data)
            except Exception:
                pass

    async def _handle_ws_binary(self, data: bytes) -> None:
        if len(data) < WS_ID_LEN:
            return
        ws_id = data[:WS_ID_LEN].rstrip(b"\0").decode("utf-8")
        websocket = self._ws_connections.get(ws_id)
        if websocket is not None:
            try:
                await websocket.send(data[WS_ID_LEN:])
            except Exception:
                pass

    async def _handle_ws_close(self, msg: dict[str, Any]) -> None:
        websocket = self._ws_connections.pop(msg.get("id", ""), None)
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass
