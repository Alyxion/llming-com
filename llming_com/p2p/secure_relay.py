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
import secrets
from collections.abc import Awaitable, Callable

from llming_com.p2p.proxy import DataChannelProxy
from llming_com.secure import (
    EphemeralKey,
    HostIdentity,
    SecureFramer,
    _b64u_decode,
    derive_session_key,
    session_info,
)

logger = logging.getLogger(__name__)

SID_LEN = 16
KIND_HELLO = b"H"
KIND_APP = b"A"
KIND_CLOSE = b"C"
KIND_RESPONSE = b"R"  # host → device: signed ephemeral + canary probe

PROBE_PREFIX = b"llming-probe-v2:"

LinkSend = Callable[[bytes], Awaitable[None]]


class _AppLink:
    """SecureFramer's transport ``peer``: prepends the ``'A'`` app-frame tag."""

    def __init__(self, send: LinkSend) -> None:
        self._send = send

    async def send(self, blob: bytes) -> None:
        await self._send(KIND_APP + blob)


class SecureProxySession:
    """One end-to-end-encrypted proxy session over a byte link.

    Transport-agnostic: the SAME session logic runs over a relay WebSocket *and*
    over a WebRTC DataChannel — only the byte link differs.  This is what binds
    BOTH transports to the host-screen code.

    Wire frames are ``kind || body``: ``H`` hello (device→host, ``{epub}``),
    ``R`` response (host→device, signed ephemeral + canary probe), ``A`` sealed
    app frame (both ways), ``C`` close.  ``send`` writes one ``kind||body`` frame
    to the link.
    """

    def __init__(
        self,
        send: LinkSend,
        identity: HostIdentity,
        *,
        local_base: str,
        code: str,
        forwarded_via: str = "proxy",
        on_pair_request: Callable[[], Awaitable[None]] | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._send = send
        self._identity = identity
        self._local_base = local_base
        self._code = code  # the host-screen code; the relay never sees it
        self._forwarded_via = forwarded_via
        # on_pair_request fires when a device starts connecting (hello) — the host
        # can show the pairing QR then. on_connected fires when the device's first
        # encrypted frame proves it has the code — the host can hide the QR.
        self._on_pair_request = on_pair_request
        self._on_connected = on_connected
        self._connected_fired = False
        self._framer: SecureFramer | None = None
        self._proxy: DataChannelProxy | None = None

    @property
    def established(self) -> bool:
        return self._proxy is not None

    async def feed(self, frame: bytes | str) -> None:
        raw = frame if isinstance(frame, bytes) else frame.encode("latin-1")
        if not raw:
            return
        kind, body = raw[:1], raw[1:]
        if kind == KIND_HELLO:
            await self._handshake(body)
        elif kind == KIND_APP and self._proxy is not None and self._framer is not None:
            try:
                decoded = self._framer.feed(body)
            except Exception as exc:  # tampered/garbage ciphertext (or wrong code)
                logger.debug("secure session frame rejected: %s", exc)
                return
            if not self._connected_fired:  # first valid frame ⇒ the device has the code
                self._connected_fired = True
                await self._fire(self._on_connected)
            await self._proxy.handle_message(decoded)
        elif kind == KIND_CLOSE:
            await self.stop()

    @staticmethod
    async def _fire(cb: Callable[[], Awaitable[None]] | None) -> None:
        if cb is None:
            return
        try:
            await cb()
        except Exception as exc:  # never let a UI hook break the session
            logger.debug("secure session callback error: %s", exc)

    async def _handshake(self, body: bytes) -> None:
        await self._fire(self._on_pair_request)  # a device is connecting → show the code
        # v2: ephemeral×ephemeral ECDH (PFS) + host-screen-code binding. Sign our
        # ephemeral so the device authenticates us against the pinned #hk key, and
        # seal a canary probe under the code-bound key so the device can verify
        # its code (and detect rotation) before any app traffic.
        try:
            device_eph = json.loads(body).get("epub", "")
            eph_h = EphemeralKey.generate()
            shared = eph_h.shared_secret(device_eph)
            info = session_info(self._identity.public_b64, device_eph, eph_h.public_b64)
            channel = derive_session_key(shared, self._code, info=info)
        except Exception as exc:  # bad/forged hello — drop
            logger.debug("secure session hello rejected: %s", exc)
            return
        nonce = secrets.token_hex(8)
        response = json.dumps(
            {
                "v": 2,
                "epub": eph_h.public_b64,
                "sig": self._identity.sign(_b64u_decode(eph_h.public_b64)),
                "nonce": nonce,
                "probe": channel.seal_b64(PROBE_PREFIX + nonce.encode()),
            }
        )
        await self._send(KIND_RESPONSE + response.encode("utf-8"))
        self._framer = SecureFramer(_AppLink(self._send), channel)
        self._proxy = DataChannelProxy(self._framer, local_base=self._local_base, forwarded_via=self._forwarded_via)
        await self._proxy.start()
        logger.debug("secure session established (v2)")

    async def stop(self) -> None:
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None


class SecureRelayHost:
    """Multiplex many :class:`SecureProxySession` over one relay link, keyed by
    the per-browser session id (``sid``).  Inbound frames are ``sid || kind ||
    body``; the sid is stripped before the session sees ``kind || body``.
    """

    def __init__(
        self,
        send: LinkSend,
        identity: HostIdentity,
        *,
        local_base: str,
        code: str,
        forwarded_via: str = "proxy",
        on_pair_request: Callable[[], Awaitable[None]] | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._send = send
        self._identity = identity
        self._local_base = local_base
        self._code = code
        self._forwarded_via = forwarded_via
        self._on_pair_request = on_pair_request
        self._on_connected = on_connected
        self._sessions: dict[str, SecureProxySession] = {}

    async def feed(self, frame: bytes) -> None:
        if len(frame) < SID_LEN + 1:
            return
        sid_bytes = frame[:SID_LEN]
        sid = sid_bytes.decode("ascii", "replace")
        rest = frame[SID_LEN:]  # kind || body
        session = self._sessions.get(sid)
        if session is None:
            session = SecureProxySession(
                _make_sid_send(self._send, sid_bytes),
                self._identity,
                local_base=self._local_base,
                code=self._code,
                forwarded_via=self._forwarded_via,
                on_pair_request=self._on_pair_request,
                on_connected=self._on_connected,
            )
            self._sessions[sid] = session
        await session.feed(rest)
        if rest[:1] == KIND_CLOSE:
            self._sessions.pop(sid, None)

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()

    @property
    def active_sessions(self) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.established]


def _make_sid_send(send: LinkSend, sid: bytes) -> LinkSend:
    async def _send(frame: bytes) -> None:
        await send(sid + frame)

    return _send


async def run_secure_relay_host(
    relay_ws_url: str,
    *,
    identity: HostIdentity,
    local_base: str,
    code: str,
    connection_key: str = "",
    forwarded_via: str = "proxy",
    on_pair_request: Callable[[], Awaitable[None]] | None = None,
    on_connected: Callable[[], Awaitable[None]] | None = None,
    reconnect: bool = True,
    reconnect_delay: float = 2.0,
) -> None:
    """Connect a :class:`SecureRelayHost` to the hub's blind relay over a WebSocket.

    ``relay_ws_url`` is the bare host endpoint, e.g.
    ``wss://hub.openhort.ai/securerelay/{host_id}/r/{room}/host``. Pass
    ``connection_key`` separately: it is sent as the ``X-OpenHort-Connection-Key``
    header so it never lands in URLs or access logs (the hub still accepts a
    legacy ``?key=`` for hosts that embed it in the URL). Runs until cancelled,
    reconnecting on transient drops so a phone can attach at any time.
    """

    import websockets

    headers = {"X-OpenHort-Connection-Key": connection_key} if connection_key else {}

    while True:
        try:
            async with websockets.connect(relay_ws_url, max_size=None, additional_headers=headers) as ws:

                async def _send(frame: bytes) -> None:
                    await ws.send(frame)

                host = SecureRelayHost(
                    _send, identity, local_base=local_base, code=code, forwarded_via=forwarded_via,
                    on_pair_request=on_pair_request, on_connected=on_connected,
                )
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
