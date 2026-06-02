"""Server-side WebRTC peer for browser P2P connections.

A thin, application-neutral wrapper around :mod:`aiortc`.  It accepts a browser
SDP offer, exposes the negotiated ``RTCDataChannel`` as a minimal ``send()`` /
``on_message`` surface, and returns the SDP answer.  Combined with
:class:`llming_com.p2p.proxy.DataChannelProxy` it gives a full browser-to-host
HTTP/WebSocket tunnel without any inbound firewall rule on the host.

``aiortc`` is an optional dependency.  Install it with::

    pip install "llming-com[webrtc]"

Typical wiring (see :func:`llming_com.p2p.host.create_p2p_host_app`)::

    peer = WebRTCPeer()
    proxy = DataChannelProxy(peer, local_base="http://127.0.0.1:8000")
    peer.on_message = proxy.handle_message
    peer.on_open = proxy.start
    answer_sdp = await peer.accept_offer(offer_sdp)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MessageHandler = Callable[[bytes | str], Awaitable[None]]
StateHandler = Callable[[str], Awaitable[None]]
OpenHandler = Callable[[], Awaitable[None]]

DEFAULT_STUN_SERVERS = ["stun:stun.l.google.com:19302"]


def _require_aiortc() -> Any:
    try:
        import aiortc  # type: ignore[import-not-found,import-untyped]  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without aiortc
        raise RuntimeError(
            "WebRTCPeer requires the optional 'aiortc' dependency. "
            "Install it with: pip install 'llming-com[webrtc]'"
        ) from exc
    return aiortc


class WebRTCPeer:
    """One server-side ``RTCPeerConnection`` driven by a browser offer.

    The ``peer`` shape expected by :class:`DataChannelProxy` is satisfied here:
    an async :meth:`send` that writes to the open DataChannel.  Inbound frames
    are delivered to :attr:`on_message`.
    """

    def __init__(
        self,
        *,
        on_message: MessageHandler | None = None,
        on_open: OpenHandler | None = None,
        on_state_change: StateHandler | None = None,
        stun_servers: list[str] | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        aiortc = _require_aiortc()
        self.on_message = on_message
        self.on_open = on_open
        self.on_state_change = on_state_change
        # Full ICE specs (TURN with username/credential) take precedence over the
        # plain STUN URL list, so the host can relay via TURN when a direct path
        # can't be punched (e.g. symmetric carrier-grade NAT on cellular).
        if ice_servers:
            built = [
                aiortc.RTCIceServer(
                    urls=spec["urls"],
                    username=spec.get("username"),
                    credential=spec.get("credential"),
                )
                for spec in ice_servers
            ]
        else:
            built = [aiortc.RTCIceServer(urls=url) for url in (stun_servers or DEFAULT_STUN_SERVERS)]
        self._pc = aiortc.RTCPeerConnection(aiortc.RTCConfiguration(iceServers=built))
        self._channel: Any = None
        self._connected = asyncio.Event()
        self._closed = False
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._pc.on("datachannel")  # type: ignore[misc]
        def _on_datachannel(channel: Any) -> None:
            self._channel = channel
            logger.debug("datachannel opened: %s", channel.label)

            @channel.on("message")  # type: ignore[misc]
            async def _on_message(message: bytes | str) -> None:
                if self.on_message is not None:
                    await self.on_message(message)

            @channel.on("open")  # type: ignore[misc]
            def _on_open() -> None:
                self._connected.set()
                if self.on_open is not None:
                    asyncio.ensure_future(self.on_open())

            @channel.on("close")  # type: ignore[misc]
            def _on_close() -> None:
                self._connected.clear()

        @self._pc.on("connectionstatechange")  # type: ignore[misc]
        async def _on_state() -> None:
            state = self._pc.connectionState
            logger.debug("connection state: %s", state)
            if self.on_state_change is not None:
                await self.on_state_change(state)
            if state in ("failed", "closed"):
                self._connected.clear()

    def add_track(self, track: Any) -> None:
        """Attach an optional media track (e.g. a video stream) before answering."""

        self._pc.addTrack(track)

    async def accept_offer(self, sdp: str, sdp_type: str = "offer") -> str:
        """Apply the browser offer and return the SDP answer string."""

        aiortc = _require_aiortc()
        await self._pc.setRemoteDescription(aiortc.RTCSessionDescription(sdp=sdp, type=sdp_type))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        await self._wait_ice_complete()
        return self._pc.localDescription.sdp

    async def _wait_ice_complete(self, timeout: float = 2.0) -> None:
        if self._pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @self._pc.on("icegatheringstatechange")  # type: ignore[misc]
        def _on_ice() -> None:
            if self._pc.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            logger.debug("ICE gathering did not complete within %.1fs; answering with partial candidates", timeout)

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False

    async def send(self, data: bytes | str) -> None:
        if self._channel is not None and self._channel.readyState == "open":
            self._channel.send(data)

    async def send_json(self, obj: dict[str, Any]) -> None:
        await self.send(json.dumps(obj))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._pc.close()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def connection_state(self) -> str:
        return self._pc.connectionState


class WebRTCPeerRegistry:
    """Tracks one :class:`WebRTCPeer` per browser session and reaps dead ones."""

    def __init__(self) -> None:
        self._peers: dict[str, WebRTCPeer] = {}

    async def create_peer(
        self,
        session_id: str,
        offer_sdp: str,
        *,
        on_message: MessageHandler | None = None,
        on_open: OpenHandler | None = None,
        on_state_change: StateHandler | None = None,
        stun_servers: list[str] | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> tuple[WebRTCPeer, str]:
        """Create (or replace) a session's peer, answer the offer, return (peer, answer)."""

        existing = self._peers.pop(session_id, None)
        if existing is not None:
            await existing.close()

        async def _state(state: str) -> None:
            if state in ("failed", "closed"):
                self._peers.pop(session_id, None)
            if on_state_change is not None:
                await on_state_change(state)

        peer = WebRTCPeer(
            on_message=on_message,
            on_open=on_open,
            on_state_change=_state,
            stun_servers=stun_servers,
            ice_servers=ice_servers,
        )
        self._peers[session_id] = peer
        answer_sdp = await peer.accept_offer(offer_sdp)
        return peer, answer_sdp

    def get_peer(self, session_id: str) -> WebRTCPeer | None:
        return self._peers.get(session_id)

    async def close_all(self) -> None:
        for peer in list(self._peers.values()):
            await peer.close()
        self._peers.clear()

    @property
    def active_sessions(self) -> list[str]:
        return [sid for sid, peer in self._peers.items() if peer.is_connected]

    def __len__(self) -> int:
        return len(self._peers)
