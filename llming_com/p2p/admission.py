"""Deployment-neutral P2P relay admission client for llming applications."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request
from urllib.parse import quote, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class RoomRegistration:
    """Relay-side grant for one room."""

    room: str
    expires_at: int
    config: dict[str, Any]


class P2PAdmissionError(RuntimeError):
    """Raised when relay admission or relay mailbox calls fail."""


class P2PAdmissionClient:
    """Small client shared by managed and private relay deployments.

    Host/app code should only need an endpoint and a host admission key.  The
    endpoint may be the managed relay, a combined hub relay path, or a private
    self-hosted relay following the same HTTP contract.
    """

    def __init__(self, endpoint: str, admission_key: str = "") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.admission_key = admission_key

    def room_http_url(self, room: str, action: str = "", **query: str) -> str:
        path = f"{self.endpoint}/{quote(room, safe='')}"
        if action:
            path = f"{path}/{action}"
        if query:
            path = f"{path}?{urlencode(query)}"
        return path

    def room_ws_url(self, room: str) -> str:
        parsed = urlparse(self.room_http_url(room))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, parsed.path, "", "", ""))

    def pairing_url(self, pair_base: str, pairing_token: str, *, param: str = "pt") -> str:
        """Return a bootstrap-only pairing URL with the token in the fragment."""

        return f"{pair_base}#{urlencode({param: pairing_token})}"

    def viewer_url(
        self,
        viewer_base: str,
        room: str,
        *,
        token: str = "",
        pair: bool = False,
        device: str = "",
        name: str = "",
        icon: str = "",
    ) -> str:
        """Return the legacy direct viewer URL.

        New browser/mobile pairing flows should prefer :meth:`pairing_url` and
        exchange the opaque token for viewer credentials server-side.
        """

        params: dict[str, str] = {
            "signal": "ws",
            "room": room,
            "relay": self.room_ws_url("").rsplit("/", 1)[0],
        }
        if token:
            params["token"] = token
        if pair:
            params["pair"] = "1"
        if device:
            params["device"] = device
        if name:
            params["name"] = name
        if icon:
            params["icon"] = icon
        return f"{viewer_base}?{urlencode(params)}"

    async def register_room(
        self,
        room: str,
        *,
        app_id: str = "",
        app_name: str = "",
        ttl_ms: int = 3_600_000,
    ) -> RoomRegistration:
        body = await self._request(
            self.room_http_url(room, "register"),
            method="POST",
            body={"app_id": app_id, "app_name": app_name, "ttl_ms": ttl_ms},
            authorized=True,
        )
        return RoomRegistration(
            room=str(body.get("room") or room),
            expires_at=int(body.get("expires_at") or 0),
            config=dict(body.get("config") or {}),
        )

    async def connect(self, room: str, device_token_hash: str) -> dict[str, Any]:
        return await self._request(
            self.room_http_url(room, "connect"),
            method="POST",
            body={"device_token_hash": device_token_hash},
        )

    async def pending(self, room: str) -> list[dict[str, Any]]:
        body = await self._request(
            self.room_http_url(room, "pending"),
            authorized=True,
        )
        requests_value = body.get("requests", [])
        return requests_value if isinstance(requests_value, list) else []

    async def respond(self, room: str, device_token_hash: str, url: str) -> dict[str, Any]:
        return await self._request(
            self.room_http_url(room, "respond"),
            method="POST",
            body={"device_token_hash": device_token_hash, "url": url},
            authorized=True,
        )

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        authorized: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request_sync,
            url,
            method=method,
            body=body,
            authorized=authorized,
        )

    def _request_sync(
        self,
        url: str,
        *,
        method: str,
        body: dict[str, Any] | None,
        authorized: bool,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "llming-com/0.1 P2PAdmissionClient",
        }
        if authorized:
            if not self.admission_key:
                raise P2PAdmissionError("missing relay admission key")
            headers["authorization"] = f"Bearer {self.admission_key}"
        req = request.Request(url, method=method, data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise P2PAdmissionError(f"relay request failed: {exc.code} {raw}") from exc
