"""Generic remote access tunnel primitives for llming applications.

The module is intentionally application-agnostic.  A host process keeps one
outbound WebSocket open to a public access hub.  Browser HTTP requests and
browser WebSocket frames are wrapped as JSON messages and relayed over that
host tunnel, so no inbound firewall/NAT rule is required on the host network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

SESSION_COOKIE = "llming_access_session"
VERIFY_TOKEN_PATH = "/_llming/remote/verify-token"
REQUEST_TIMEOUT = 30.0
TUNNEL_BODY_CHUNK_SIZE = 32_000


def hash_password(password: str) -> str:
    """Hash *password* with PBKDF2-SHA256."""

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2_sha256$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time password verification."""

    try:
        scheme, rounds_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return hmac.compare_digest(actual, expected)


@dataclass
class AccessUser:
    username: str
    password_hash: str
    display_name: str = ""


@dataclass
class AccessHost:
    host_id: str
    owner: str
    connection_key: str
    display_name: str = ""


@dataclass
class _BrowserSocket:
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop


class InMemoryAccessStore:
    """Small access store suitable for tests and embedded single-process apps.

    Production deployments should back the same interface with durable storage
    such as Cloudflare KV, Redis, Postgres, or a file with process locking.
    """

    def __init__(self) -> None:
        self.users: dict[str, AccessUser] = {}
        self.hosts: dict[str, AccessHost] = {}
        self.hosts_by_key: dict[str, str] = {}
        self.sessions: dict[str, str] = {}

    def create_user(self, username: str, password: str, display_name: str = "") -> AccessUser:
        user = AccessUser(username=username, password_hash=hash_password(password), display_name=display_name)
        self.users[username] = user
        return user

    def get_user(self, username: str) -> AccessUser | None:
        return self.users.get(username)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = username
        return token

    def get_session_user(self, token: str) -> str | None:
        return self.sessions.get(token)

    def clear_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    def create_host(self, owner: str, display_name: str = "") -> AccessHost:
        host_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
        connection_key = secrets.token_urlsafe(32)
        host = AccessHost(
            host_id=host_id,
            owner=owner,
            connection_key=connection_key,
            display_name=display_name or host_id,
        )
        self.hosts[host_id] = host
        self.hosts_by_key[connection_key] = host_id
        return host

    def get_host(self, host_id: str) -> AccessHost | None:
        return self.hosts.get(host_id)

    def get_host_by_key(self, connection_key: str) -> AccessHost | None:
        host_id = self.hosts_by_key.get(connection_key, "")
        return self.hosts.get(host_id)

    def hosts_for_user(self, username: str) -> list[AccessHost]:
        return [host for host in self.hosts.values() if host.owner == username]


class RateLimiter:
    """Tiny in-memory auth limiter."""

    def __init__(self, limit: int = 10, window: float = 300.0) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        now = time.monotonic()
        attempts = [ts for ts in self._attempts.get(key, []) if now - ts < self.window]
        self._attempts[key] = attempts
        return len(attempts) < self.limit

    def record(self, key: str) -> None:
        self._attempts.setdefault(key, []).append(time.monotonic())


class HostTunnel:
    """One connected internal host.

    Starlette WebSockets cannot be sent to concurrently from arbitrary tasks,
    so all outbound messages to the host go through ``_send_queue`` and a
    single writer coroutine.
    """

    def __init__(self, host_id: str, display_name: str, websocket: WebSocket) -> None:
        self.host_id = host_id
        self.display_name = display_name
        self.websocket = websocket
        self.connected_at = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._chunked: dict[str, dict[str, Any]] = {}
        self._send_queue: asyncio.Queue[str] = asyncio.Queue()
        self._ws_clients: dict[str, _BrowserSocket] = {}

    async def proxy_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        req_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future
        await self.send_raw(json.dumps({
            "type": "http_request",
            "req_id": req_id,
            "method": method,
            "path": path,
            "headers": headers,
            "body": body.decode("latin-1") if body else "",
        }))
        try:
            response = await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
            if "body_b64" in response:
                response["body_bytes"] = base64.b64decode(response.pop("body_b64"))
            elif "body" in response:
                response["body_bytes"] = str(response["body"]).encode("utf-8")
            else:
                response["body_bytes"] = b""
            return response
        finally:
            self._pending.pop(req_id, None)

    async def send_raw(self, payload: str) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            await self._send_queue.put(payload)
            return
        future = asyncio.run_coroutine_threadsafe(self._send_queue.put(payload), self._loop)
        await asyncio.wrap_future(future)

    async def run(self) -> None:
        async def reader() -> None:
            while True:
                raw = await self.websocket.receive_text()
                self._handle_message(json.loads(raw))

        async def writer() -> None:
            while True:
                await self.websocket.send_text(await self._send_queue.get())

        tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                task.result()
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("host tunnel disconnected"))
            self._pending.clear()
            for browser in list(self._ws_clients.values()):
                try:
                    await self._call_browser(browser, "close", code=1001, reason="Host tunnel disconnected")
                except Exception:
                    pass
            self._ws_clients.clear()

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "http_response":
            self._resolve_response(msg.get("req_id", ""), msg)
        elif msg_type == "http_response_start":
            req_id = msg.get("req_id", "")
            self._chunked[req_id] = {
                "status": msg.get("status", 200),
                "headers": msg.get("headers", {}),
                "total": msg.get("total_chunks", 1),
                "chunks": {msg.get("chunk_index", 0): msg.get("chunk", "")},
            }
            if msg.get("total_chunks", 1) == 1:
                self._finish_chunked(req_id)
        elif msg_type == "http_response_chunk":
            req_id = msg.get("req_id", "")
            chunked = self._chunked.get(req_id)
            if chunked:
                chunked["chunks"][msg.get("chunk_index", 0)] = msg.get("chunk", "")
                if len(chunked["chunks"]) >= chunked["total"]:
                    self._finish_chunked(req_id)
        elif msg_type == "ws_data":
            asyncio.create_task(self._forward_ws_data(msg))
        elif msg_type == "ws_close":
            ws_id = msg.get("ws_id", "")
            websocket = self._ws_clients.pop(ws_id, None)
            if websocket:
                asyncio.create_task(self._call_browser(websocket, "close", code=1000))

    def _finish_chunked(self, req_id: str) -> None:
        chunked = self._chunked.pop(req_id, None)
        if not chunked:
            return
        body_b64 = "".join(chunked["chunks"][i] for i in range(chunked["total"]))
        self._resolve_response(req_id, {
            "req_id": req_id,
            "status": chunked["status"],
            "headers": chunked["headers"],
            "body_b64": body_b64,
        })

    def _resolve_response(self, req_id: str, response: dict[str, Any]) -> None:
        future = self._pending.get(req_id)
        if future and not future.done():
            loop = future.get_loop()
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if loop is running_loop:
                future.set_result(response)
            else:
                loop.call_soon_threadsafe(future.set_result, response)

    async def _forward_ws_data(self, msg: dict[str, Any]) -> None:
        browser = self._ws_clients.get(msg.get("ws_id", ""))
        if not browser:
            return
        try:
            if "text" in msg:
                await self._call_browser(browser, "send_text", msg["text"])
            elif "binary" in msg:
                await self._call_browser(browser, "send_bytes", base64.b64decode(msg["binary"]))
        except Exception:
            pass

    async def _call_browser(self, browser: _BrowserSocket, method: str, *args: Any, **kwargs: Any) -> Any:
        async def _invoke() -> Any:
            return await getattr(browser.websocket, method)(*args, **kwargs)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is browser.loop:
            return await _invoke()
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_invoke(), browser.loop))


class TunnelClient:
    """Outbound connector that exposes one local app through an access hub."""

    def __init__(
        self,
        access_server_url: str,
        connection_key: str,
        *,
        local_url: str = "http://127.0.0.1:8765",
        reconnect_delay: float = 5.0,
    ) -> None:
        self.access_server_url = access_server_url.rstrip("/")
        self.connection_key = connection_key
        self.local_url = local_url.rstrip("/")
        self.reconnect_delay = reconnect_delay
        self.host_id = ""
        self.display_name = ""
        self._running = False
        self._tunnel_ws: Any = None
        self._http_client: Any = None
        self._local_ws: dict[str, Any] = {}
        self._pending_ws_data: dict[str, list[dict[str, Any]]] = {}

    async def run(self) -> None:
        """Connect to the access hub and keep relaying until stopped."""

        import websockets

        self._running = True
        ws_base = self.access_server_url.replace("http://", "ws://").replace("https://", "wss://")
        tunnel_url = f"{ws_base}/api/access/tunnel?key={self.connection_key}"
        while self._running:
            try:
                async with websockets.connect(tunnel_url) as websocket:
                    self._tunnel_ws = websocket
                    await self._read_welcome(websocket)
                    await self._message_loop(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._running:
                    logger.debug("llming tunnel disconnected: %s", exc)
                    await asyncio.sleep(self.reconnect_delay)
            finally:
                self._tunnel_ws = None

    async def stop(self) -> None:
        self._running = False
        if self._tunnel_ws is not None:
            try:
                await self._tunnel_ws.close()
            except Exception:
                pass
        for websocket in list(self._local_ws.values()):
            try:
                await websocket.close()
            except Exception:
                pass
        self._local_ws.clear()
        self._pending_ws_data.clear()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _read_welcome(self, websocket: Any) -> None:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") == "welcome":
            self.host_id = msg.get("host_id", "")
            self.display_name = msg.get("display_name", "")

    async def _message_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            msg_type = msg.get("type", "")
            if msg_type == "http_request":
                asyncio.create_task(self._handle_http(websocket, msg))
            elif msg_type == "ws_open":
                asyncio.create_task(self._handle_ws_open(websocket, msg))
            elif msg_type == "ws_data":
                await self._handle_ws_data(msg)
            elif msg_type == "ws_close":
                await self._handle_ws_close(msg)

    async def _handle_http(self, tunnel_ws: Any, msg: dict[str, Any]) -> None:
        import httpx

        req_id = msg.get("req_id", "")
        body = (msg.get("body") or "").encode("latin-1")
        headers = {
            k: v
            for k, v in msg.get("headers", {}).items()
            if k.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
        }
        try:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(base_url=self.local_url, timeout=30.0)
            response = await self._http_client.request(
                method=msg.get("method", "GET"),
                url=msg.get("path", "/"),
                headers=headers,
                content=body,
            )
            body_b64 = base64.b64encode(response.content).decode("ascii")
            if len(body_b64) <= TUNNEL_BODY_CHUNK_SIZE:
                await tunnel_ws.send(json.dumps({
                    "type": "http_response",
                    "req_id": req_id,
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body_b64": body_b64,
                }))
            else:
                chunks = [
                    body_b64[i:i + TUNNEL_BODY_CHUNK_SIZE]
                    for i in range(0, len(body_b64), TUNNEL_BODY_CHUNK_SIZE)
                ]
                for index, chunk in enumerate(chunks):
                    await tunnel_ws.send(json.dumps({
                        "type": "http_response_start" if index == 0 else "http_response_chunk",
                        "req_id": req_id,
                        **({
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "total_chunks": len(chunks),
                        } if index == 0 else {}),
                        "chunk_index": index,
                        "chunk": chunk,
                    }))
        except Exception as exc:
            await tunnel_ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 502,
                "headers": {"content-type": "text/plain"},
                "body": f"local app error: {exc}",
            }))

    async def _handle_ws_open(self, tunnel_ws: Any, msg: dict[str, Any]) -> None:
        import websockets

        ws_id = msg.get("ws_id", "")
        ws_base = self.local_url.replace("http://", "ws://").replace("https://", "wss://")
        try:
            local_ws = await websockets.connect(ws_base + msg.get("path", "/"))
            self._local_ws[ws_id] = local_ws
            for pending in self._pending_ws_data.pop(ws_id, []):
                await self._send_to_local_ws(local_ws, pending)
            asyncio.create_task(self._local_ws_reader(tunnel_ws, ws_id, local_ws))
        except Exception as exc:
            logger.debug("failed to open local websocket %s: %s", ws_id, exc)
            await tunnel_ws.send(json.dumps({"type": "ws_close", "ws_id": ws_id, "reason": str(exc)}))

    async def _local_ws_reader(self, tunnel_ws: Any, ws_id: str, local_ws: Any) -> None:
        try:
            async for payload in local_ws:
                if isinstance(payload, str):
                    await tunnel_ws.send(json.dumps({"type": "ws_data", "ws_id": ws_id, "text": payload}))
                else:
                    await tunnel_ws.send(json.dumps({
                        "type": "ws_data",
                        "ws_id": ws_id,
                        "binary": base64.b64encode(payload).decode("ascii"),
                    }))
        except Exception:
            pass
        finally:
            self._local_ws.pop(ws_id, None)
            try:
                await tunnel_ws.send(json.dumps({"type": "ws_close", "ws_id": ws_id}))
            except Exception:
                pass

    async def _handle_ws_data(self, msg: dict[str, Any]) -> None:
        websocket = self._local_ws.get(msg.get("ws_id", ""))
        if websocket is None:
            self._pending_ws_data.setdefault(msg.get("ws_id", ""), []).append(msg)
            return
        await self._send_to_local_ws(websocket, msg)

    async def _send_to_local_ws(self, websocket: Any, msg: dict[str, Any]) -> None:
        try:
            if "text" in msg:
                await websocket.send(msg["text"])
            elif "binary" in msg:
                await websocket.send(base64.b64decode(msg["binary"]))
        except Exception:
            pass

    async def _handle_ws_close(self, msg: dict[str, Any]) -> None:
        websocket = self._local_ws.pop(msg.get("ws_id", ""), None)
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass


def create_access_app(
    store: InMemoryAccessStore | None = None,
    *,
    verify_token_path: str = VERIFY_TOKEN_PATH,
) -> FastAPI:
    """Create an app-agnostic remote access hub.

    The returned FastAPI app is useful for local/private deployments and for
    exercising the protocol.  Production Cloudflare deployments should use the
    same message protocol in a Durable Object hub.
    """

    access_store = store or InMemoryAccessStore()
    tunnels: dict[str, HostTunnel] = {}
    limiter = RateLimiter()
    app = FastAPI(title="llming-remote-access")

    def _client_ip(request: Request) -> str:
        return request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip() or (
            request.client.host if request.client else "unknown"
        )

    def _username_from_request(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        return access_store.get_session_user(token) or ""

    def _require_user(request: Request) -> str:
        username = _username_from_request(request)
        if not username:
            raise HTTPException(401, "Not authenticated")
        return username

    @app.post("/api/access/login")
    async def login(request: Request) -> Response:
        ip = _client_ip(request)
        if not limiter.check(ip):
            raise HTTPException(429, "Too many attempts")
        limiter.record(ip)
        data = await request.json()
        user = access_store.get_user(data.get("username", ""))
        if not user or not verify_password(data.get("password", ""), user.password_hash):
            raise HTTPException(401, "Invalid credentials")
        token = access_store.create_session(user.username)
        response = JSONResponse({"ok": True, "username": user.username})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="none" if request.url.scheme == "https" else "lax",
            max_age=86_400,
        )
        return response

    @app.post("/api/access/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            access_store.clear_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/api/access/me")
    async def me(request: Request) -> Response:
        return JSONResponse({"username": _require_user(request)})

    @app.get("/api/access/hosts")
    async def list_hosts(request: Request) -> Response:
        username = _require_user(request)
        return JSONResponse({
            "hosts": [
                {
                    "host_id": host.host_id,
                    "display_name": host.display_name,
                    "online": host.host_id in tunnels,
                }
                for host in access_store.hosts_for_user(username)
            ]
        })

    @app.post("/api/access/hosts")
    async def create_host(request: Request) -> Response:
        username = _require_user(request)
        data = await request.json()
        host = access_store.create_host(username, data.get("display_name", ""))
        return JSONResponse({
            "host_id": host.host_id,
            "connection_key": host.connection_key,
            "display_name": host.display_name,
        })

    @app.get("/t/{host_id}/{token}")
    async def token_login(request: Request, host_id: str, token: str) -> Response:
        host = access_store.get_host(host_id)
        tunnel = tunnels.get(host_id)
        if not host or not tunnel:
            raise HTTPException(502, "Host not connected")
        verify_resp = await tunnel.proxy_request(
            "POST",
            verify_token_path,
            {"content-type": "application/json", "x-forwarded-via": "proxy"},
            json.dumps({"token": token}).encode(),
        )
        if verify_resp.get("status") != 200:
            raise HTTPException(401, "Invalid or expired token")
        body = json.loads(verify_resp.get("body_bytes", b"{}"))
        if not body.get("valid"):
            raise HTTPException(401, "Invalid or expired token")
        session_token = access_store.create_session(host.owner)
        response = RedirectResponse(f"/proxy/{host_id}/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="none" if request.url.scheme == "https" else "lax",
            max_age=86_400,
        )
        return response

    @app.websocket("/api/access/tunnel")
    async def host_tunnel(websocket: WebSocket) -> None:
        host = access_store.get_host_by_key(websocket.query_params.get("key", ""))
        if not host:
            await websocket.close(code=4003, reason="Invalid connection key")
            return
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "welcome",
            "host_id": host.host_id,
            "display_name": host.display_name,
        }))
        tunnel = HostTunnel(host.host_id, host.display_name, websocket)
        old = tunnels.get(host.host_id)
        if old:
            try:
                await old.websocket.close(code=1000, reason="Replaced by new tunnel")
            except Exception:
                pass
        tunnels[host.host_id] = tunnel
        try:
            await tunnel.run()
        except WebSocketDisconnect:
            pass
        finally:
            if tunnels.get(host.host_id) is tunnel:
                tunnels.pop(host.host_id, None)

    @app.websocket("/proxy/{host_id}/{path:path}")
    async def ws_proxy(websocket: WebSocket, host_id: str, path: str) -> None:
        token = websocket.cookies.get(SESSION_COOKIE, "")
        if not access_store.get_session_user(token):
            await websocket.close(code=4001, reason="Not authenticated")
            return
        tunnel = tunnels.get(host_id)
        if not tunnel:
            await websocket.close(code=4004, reason="Host not connected")
            return
        await websocket.accept()
        ws_id = secrets.token_hex(8)
        tunnel._ws_clients[ws_id] = _BrowserSocket(websocket, asyncio.get_running_loop())
        qs = str(websocket.query_params)
        actual_path = f"/{path}" + (f"?{qs}" if qs else "")
        await tunnel.send_raw(json.dumps({"type": "ws_open", "ws_id": ws_id, "path": actual_path}))
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if "text" in message and message["text"] is not None:
                    await tunnel.send_raw(json.dumps({"type": "ws_data", "ws_id": ws_id, "text": message["text"]}))
                elif "bytes" in message and message["bytes"] is not None:
                    await tunnel.send_raw(json.dumps({
                        "type": "ws_data",
                        "ws_id": ws_id,
                        "binary": base64.b64encode(message["bytes"]).decode("ascii"),
                    }))
        except WebSocketDisconnect:
            pass
        finally:
            tunnel._ws_clients.pop(ws_id, None)
            await tunnel.send_raw(json.dumps({"type": "ws_close", "ws_id": ws_id}))

    @app.api_route("/proxy/{host_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def http_proxy(request: Request, host_id: str, path: str) -> Response:
        _require_user(request)
        tunnel = tunnels.get(host_id)
        if not tunnel:
            raise HTTPException(502, "Host not connected")
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
        }
        headers["x-forwarded-via"] = "proxy"
        proxied_path = f"/{path}" + (f"?{request.url.query}" if request.url.query else "")
        tunnel_resp = await tunnel.proxy_request(request.method, proxied_path, headers, await request.body())
        body = tunnel_resp.get("body_bytes", b"")
        response_headers = {
            k: v
            for k, v in tunnel_resp.get("headers", {}).items()
            if k.lower() not in {"content-length", "transfer-encoding", "content-encoding"}
        }
        content_type = response_headers.get("content-type", response_headers.get("Content-Type", ""))
        if "text/html" in content_type:
            html = body.decode("utf-8", errors="replace")
            base = f"/proxy/{host_id}/"
            if "<head>" in html:
                html = html.replace("<head>", f'<head><base href="{base}">', 1)
            body = html.encode("utf-8")
            response_headers["content-type"] = "text/html; charset=utf-8"
        return Response(body, status_code=tunnel_resp.get("status", 200), headers=response_headers)

    app.state.llming_access_store = access_store
    app.state.llming_access_tunnels = tunnels
    return app
