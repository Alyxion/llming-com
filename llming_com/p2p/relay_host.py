"""Host-side relay loop — pure P2P over a relay room.

This is the host counterpart to the browser viewer's relay signaling
(``signal=ws``).  It completes the pure-P2P-over-relay path:

1. register a room with the relay (admission key);
2. poll the relay's SDP inbox for browser offers;
3. for each offer, create a :class:`WebRTCPeer` answer wired to a
   :class:`DataChannelProxy` (which forwards to the local app);
4. post the answer back through the relay so the browser can connect directly.

Once the DataChannel opens, all traffic is direct browser↔host — the relay is
only used for the ~4 KB SDP exchange.  Works against any relay implementing the
standard contract (the Cloudflare worker in ``server/p2p/relay`` or a private
deployment); the admission client is injectable for testing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from urllib import error, request

from llming_com.p2p.admission import P2PAdmissionClient
from llming_com.p2p.proxy import DataChannelProxy
from llming_com.p2p.webrtc import WebRTCPeerRegistry

logger = logging.getLogger(__name__)


class RelayHost:
    """Answer browser SDP offers arriving over a relay room."""

    def __init__(
        self,
        room: str,
        local_base: str,
        *,
        relay_endpoint: str = "",
        admission_key: str = "",
        admission: P2PAdmissionClient | None = None,
        stun_servers: list[str] | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
        ice_endpoint: str = "",
        sdp_poll_interval: float = 0.25,
        room_ttl_ms: int = 86_400_000,
        room_renew_interval: float = 1800.0,
        app_id: str = "",
        app_name: str = "",
    ) -> None:
        if admission is None:
            if not relay_endpoint:
                raise ValueError("provide either an admission client or a relay_endpoint")
            admission = P2PAdmissionClient(relay_endpoint, admission_key)
        self._admission = admission
        self.room = room
        self.local_base = local_base.rstrip("/")
        self._stun = stun_servers
        # Static ICE servers (TURN with credentials), or an endpoint that mints
        # fresh short-lived ones per offer.  Either makes the host reachable when
        # a direct punch fails (symmetric NAT / cellular).
        self._ice_servers = ice_servers
        self._ice_endpoint = ice_endpoint
        self._poll = sdp_poll_interval
        # Room grants are TTL-bounded by the relay (max 24h). Request the longest
        # grant and renew it on a timer so direct P2P never lapses while the host
        # runs — instead of the old register-once-then-expire-in-1h behaviour.
        self._room_ttl_ms = room_ttl_ms
        self._room_renew_interval = room_renew_interval
        self._app_id = app_id
        self._app_name = app_name
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._peers = WebRTCPeerRegistry()
        self._proxies: dict[str, DataChannelProxy] = {}

    async def start(self) -> None:
        """Register the room and begin polling for offers."""

        await self._register()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._renew_task = asyncio.create_task(self._renew_loop())
        logger.debug("relay host started for room %s", self.room)

    async def _register(self) -> None:
        await self._admission.register_room(
            self.room, app_id=self._app_id, app_name=self._app_name, ttl_ms=self._room_ttl_ms
        )

    async def _renew_loop(self) -> None:
        """Re-register before the grant lapses so the room stays active indefinitely."""

        while self._running:
            await asyncio.sleep(self._room_renew_interval)
            if not self._running:
                return
            try:
                await self._register()
                logger.debug("relay room %s grant renewed", self.room)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient — try again next interval
                logger.debug("relay room renew failed: %s", exc)

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._renew_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._renew_task = None
        await self._peers.close_all()
        for proxy in list(self._proxies.values()):
            await proxy.stop()
        self._proxies.clear()

    async def _loop(self) -> None:
        while self._running:
            try:
                messages = await self._admission.sdp_inbox(self.room)
                for message in messages:
                    if message.get("type") == "offer" and message.get("sdp"):
                        await self._handle_offer(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transient relay/network errors — keep polling
                logger.debug("relay host poll error: %s", exc)
            await asyncio.sleep(self._poll)

    async def _handle_offer(self, message: dict[str, Any]) -> None:
        session_id = str(message.get("id") or secrets.token_hex(8))
        proxy_holder: dict[str, DataChannelProxy] = {}

        async def on_message(data: bytes | str) -> None:
            proxy = proxy_holder.get("proxy")
            if proxy is not None:
                await proxy.handle_message(data)

        async def on_open() -> None:
            proxy = proxy_holder.get("proxy")
            if proxy is not None:
                await proxy.start()

        ice_servers = await self._resolve_ice_servers()
        peer, answer_sdp = await self._peers.create_peer(
            session_id,
            message["sdp"],
            on_message=on_message,
            on_open=on_open,
            stun_servers=self._stun,
            ice_servers=ice_servers,
        )
        proxy = DataChannelProxy(peer, local_base=self.local_base)
        proxy_holder["proxy"] = proxy
        self._proxies[session_id] = proxy
        await self._admission.sdp_send(self.room, {"type": "answer", "sdp": answer_sdp, "id": session_id})
        logger.debug("relay host answered offer for session %s", session_id)

    async def _resolve_ice_servers(self) -> list[dict[str, Any]] | None:
        """Fresh ICE servers per offer when an endpoint is set, else the static list.

        Mirrors what the browser viewer is handed by the hub, so both ends use the
        same STUN+TURN set and a direct path is preferred with TURN as the relay.
        """

        if not self._ice_endpoint:
            return self._ice_servers
        try:
            servers = await asyncio.to_thread(self._fetch_ice_sync, self._ice_endpoint)
            return servers or self._ice_servers
        except Exception as exc:  # endpoint down — fall back to static/STUN
            logger.debug("ice endpoint fetch failed: %s", exc)
            return self._ice_servers

    @staticmethod
    def _fetch_ice_sync(endpoint: str) -> list[dict[str, Any]] | None:
        req = request.Request(endpoint, headers={"accept": "application/json"})
        try:
            with request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (error.URLError, ValueError):
            return None
        servers = body.get("ice_servers")
        return servers if isinstance(servers, list) and servers else None

    @property
    def active_sessions(self) -> list[str]:
        return self._peers.active_sessions
