"""Host side of the **encrypted blind-relay** proxy transport.

When a direct/TURN WebRTC path can't be established, the browser falls back to
relaying the DataChannelProxy protocol through the hub over a WebSocket — but
**end-to-end encrypted**, so the hub forwards only ciphertext (it is a *blind*
relay, not a plaintext-terminating proxy).

Wire protocol
-------------

The hub relay is dumb: it pairs one host link with many browser links and
multiplexes them by a per-browser **session id** (``sid``).  It prepends the
``sid`` on frames going to the host and strips it on frames going to the browser
— never reading the body.

Frames (binary)::

    host ⇄ relay :  sid(16 ascii) || kind(1) || body
    relay ⇄ browser :          kind(1) || body      (relay adds/strips sid)

``kind``:

- ``H`` *hello*  — body = plaintext JSON ``{"epub": <browser ephemeral pubkey>}``
  (browser → host, first frame of a session).  The host derives the AES-GCM
  session key from its long-term identity key and the browser's ephemeral key.
- ``A`` *app*    — body = a sealed DataChannelProxy frame (both directions).
- ``C`` *close*  — body empty (either direction).

Host authentication is out-of-band: the browser pinned the host key fingerprint
from the pairing QR's URL fragment (``#hk=``) before connecting, so a malicious
relay swapping the key is detected and the derived key won't match.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from llming_com.p2p.proxy import DataChannelProxy
from llming_com.secure import HostIdentity, SecureChannel, SecureFramer

logger = logging.getLogger(__name__)

SID_LEN = 16
KIND_HELLO = b"H"
KIND_APP = b"A"
KIND_CLOSE = b"C"

LinkSend = Callable[[bytes], Awaitable[None]]


class _SidLink:
    """A per-session view of the relay link that tags frames with the sid.

    Presented to :class:`SecureFramer` as its transport ``peer``: the framer
    seals a DataChannelProxy frame, and this prepends ``sid || 'A'`` so the
    relay routes it to the right browser.
    """

    def __init__(self, send: LinkSend, sid: bytes) -> None:
        self._send = send
        self._prefix = sid + KIND_APP

    async def send(self, blob: bytes) -> None:
        await self._send(self._prefix + blob)


class SecureRelayHost:
    """Terminate many encrypted browser sessions arriving over one relay link.

    ``send`` is how this host writes a frame toward the relay (e.g. a WebSocket
    ``send``).  Feed inbound relay frames to :meth:`feed`.  One
    :class:`DataChannelProxy` is created per browser session and forwards to the
    local app exactly like the P2P path — the only difference is the transport
    and the encryption layer.
    """

    def __init__(
        self,
        send: LinkSend,
        identity: HostIdentity,
        *,
        local_base: str,
        forwarded_via: str = "proxy",
    ) -> None:
        self._send = send
        self._identity = identity
        self._local_base = local_base
        self._forwarded_via = forwarded_via
        self._sessions: dict[str, tuple[DataChannelProxy, SecureFramer, SecureChannel]] = {}

    async def feed(self, frame: bytes) -> None:
        """Handle one ``sid || kind || body`` frame from the relay."""

        if len(frame) < SID_LEN + 1:
            return
        sid = frame[:SID_LEN].decode("ascii", "replace")
        kind = frame[SID_LEN : SID_LEN + 1]
        body = frame[SID_LEN + 1 :]
        if kind == KIND_HELLO:
            await self._open(sid, frame[:SID_LEN], body)
        elif kind == KIND_APP:
            await self._app(sid, body)
        elif kind == KIND_CLOSE:
            await self._close(sid)

    async def _open(self, sid: str, sid_bytes: bytes, body: bytes) -> None:
        try:
            epub = json.loads(body).get("epub", "")
            channel = self._identity.derive(epub)
        except Exception as exc:  # bad/forged hello — drop the session
            logger.debug("secure relay hello rejected for %s: %s", sid, exc)
            return
        framer = SecureFramer(_SidLink(self._send, sid_bytes), channel)
        proxy = DataChannelProxy(framer, local_base=self._local_base, forwarded_via=self._forwarded_via)
        await proxy.start()
        self._sessions[sid] = (proxy, framer, channel)
        logger.debug("secure relay session opened: %s", sid)

    async def _app(self, sid: str, body: bytes) -> None:
        session = self._sessions.get(sid)
        if session is None:
            return
        proxy, framer, _ = session
        try:
            frame = framer.feed(body)
        except Exception as exc:  # tampered/garbage ciphertext
            logger.debug("secure relay frame rejected for %s: %s", sid, exc)
            return
        await proxy.handle_message(frame)

    async def _close(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is not None:
            await session[0].stop()

    async def stop(self) -> None:
        for proxy, _, _ in list(self._sessions.values()):
            await proxy.stop()
        self._sessions.clear()

    @property
    def active_sessions(self) -> list[str]:
        return list(self._sessions)


async def run_secure_relay_host(
    relay_ws_url: str,
    *,
    identity: HostIdentity,
    local_base: str,
    forwarded_via: str = "proxy",
    reconnect: bool = True,
    reconnect_delay: float = 2.0,
) -> None:
    """Connect a :class:`SecureRelayHost` to the hub's blind relay over a WebSocket.

    ``relay_ws_url`` is the host endpoint, e.g.
    ``wss://hub.openhort.ai/securerelay/{host_id}/host?key=<connection_key>``.
    Runs until cancelled, reconnecting on transient drops so a phone can attach
    at any time.
    """

    import websockets

    while True:
        try:
            async with websockets.connect(relay_ws_url, max_size=None) as ws:

                async def _send(frame: bytes) -> None:
                    await ws.send(frame)

                host = SecureRelayHost(_send, identity, local_base=local_base, forwarded_via=forwarded_via)
                logger.debug("secure relay host connected")
                try:
                    async for message in ws:
                        raw = message if isinstance(message, bytes) else message.encode("latin-1")
                        await host.feed(raw)
                finally:
                    await host.stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # transient relay/network error — reconnect
            logger.debug("secure relay host connection error: %s", exc)
        if not reconnect:
            return
        await asyncio.sleep(reconnect_delay)
