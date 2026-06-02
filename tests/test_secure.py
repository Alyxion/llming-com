"""Tests for the end-to-end encryption core (host-authenticated ECDH + AES-GCM)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request

from llming_com import EphemeralKey, HostIdentity, SecureChannel, fingerprint
from llming_com.secure import _b64u_decode, _b64u_encode


def _ping_app() -> FastAPI:
    # Defined at module scope so FastAPI can resolve the `Request` hint under
    # `from __future__ import annotations` (it can't resolve a function-local import).
    app = FastAPI()

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, str]:
        return {"ok": "yes", "via": request.headers.get("x-forwarded-via", "")}

    return app


def test_ecdh_both_sides_derive_same_key() -> None:
    host = HostIdentity.generate()
    client = EphemeralKey.generate()
    host_chan = host.derive(client.public_b64)
    client_chan = client.derive(host.public_b64)
    sealed = client_chan.seal(b"hello relay-blind world")
    assert host_chan.open(sealed) == b"hello relay-blind world"
    # and the other direction
    back = host_chan.seal(b"pong")
    assert client_chan.open(back) == b"pong"


def test_seal_is_nondeterministic_but_roundtrips() -> None:
    chan = SecureChannel(b"\x01" * 32)
    a, b = chan.seal(b"x"), chan.seal(b"x")
    assert a != b  # random nonce per message
    assert chan.open(a) == chan.open(b) == b"x"


def test_tamper_is_rejected() -> None:
    chan = SecureChannel(b"\x02" * 32)
    blob = bytearray(chan.seal(b"secret"))
    blob[-1] ^= 0x01
    with pytest.raises(Exception):
        chan.open(bytes(blob))


def test_aad_must_match() -> None:
    chan = SecureChannel(b"\x03" * 32)
    sealed = chan.seal(b"body", aad=b"req-7")
    assert chan.open(sealed, aad=b"req-7") == b"body"
    with pytest.raises(Exception):
        chan.open(sealed, aad=b"req-8")


def test_wrong_peer_cannot_decrypt() -> None:
    host = HostIdentity.generate()
    client = EphemeralKey.generate()
    attacker = HostIdentity.generate()
    sealed = client.derive(host.public_b64).seal(b"top secret")
    # Attacker derives against the client's pubkey with its own key -> different secret.
    with pytest.raises(Exception):
        attacker.derive(client.public_b64).open(sealed)


def test_host_identity_pem_roundtrip_stable_fingerprint() -> None:
    host = HostIdentity.generate()
    pem = host.to_pem()
    restored = HostIdentity.from_pem(pem)
    assert restored.public_b64 == host.public_b64
    assert restored.fingerprint == host.fingerprint


def test_fingerprint_pins_the_public_key() -> None:
    host = HostIdentity.generate()
    other = HostIdentity.generate()
    assert fingerprint(host.public_b64) == host.fingerprint
    assert fingerprint(other.public_b64) != host.fingerprint  # swap is detectable
    assert len(host.fingerprint) == 16


def test_b64url_helpers_roundtrip() -> None:
    for raw in (b"", b"\x00", b"abc", bytes(range(50))):
        assert _b64u_decode(_b64u_encode(raw)) == raw


def test_seal_b64_helpers() -> None:
    chan = SecureChannel(b"\x04" * 32)
    token = chan.seal_b64(b"json-frame", aad=b"v1")
    assert isinstance(token, str)
    assert chan.open_b64(token, aad=b"v1") == b"json-frame"


def test_secure_framer_roundtrips_str_and_bytes() -> None:
    from llming_com import SecureFramer

    class _Sink:
        def __init__(self) -> None:
            self.out: list[bytes] = []

        async def send(self, blob: object) -> None:
            self.out.append(blob)  # type: ignore[arg-type]

    chan = SecureChannel(b"\x05" * 32)
    sink = _Sink()
    framer = SecureFramer(sink, chan)

    import asyncio

    async def run() -> None:
        await framer.send('{"type":"http"}')      # JSON control frame
        await framer.send(b"\x01\x02\x03binary")   # WS binary frame

    asyncio.run(run())
    assert sink.out[0] != b'{"type":"http"}'        # on the wire it's ciphertext
    assert framer.feed(sink.out[0]) == '{"type":"http"}'
    assert framer.feed(sink.out[1]) == b"\x01\x02\x03binary"


@pytest.mark.asyncio
async def test_encrypted_datachannelproxy_end_to_end() -> None:
    """Client seals an HTTP frame; host opens it, proxies to a real app, seals
    the response back. Only ciphertext crosses the (simulated) relay."""

    import asyncio
    import json
    import socket
    import threading
    import time

    import uvicorn

    from llming_com import EphemeralKey, HostIdentity, SecureFramer
    from llming_com.p2p.proxy import DataChannelProxy

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(_ping_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not getattr(server, "started", False):
        time.sleep(0.02)

    # Host-authenticated ECDH: client pins host pubkey, both derive the session key.
    host = HostIdentity.generate()
    client = EphemeralKey.generate()
    host_chan = host.derive(client.public_b64)
    client_chan = client.derive(host.public_b64)

    host_to_client: asyncio.Queue[bytes] = asyncio.Queue()
    client_to_host: asyncio.Queue[bytes] = asyncio.Queue()

    class _QPeer:
        def __init__(self, q: asyncio.Queue[bytes]) -> None:
            self.q = q

        async def send(self, blob: bytes) -> None:
            await self.q.put(blob)

    host_framer = SecureFramer(_QPeer(host_to_client), host_chan)
    client_framer = SecureFramer(_QPeer(client_to_host), client_chan)
    proxy = DataChannelProxy(host_framer, local_base=f"http://127.0.0.1:{port}")
    await proxy.start()
    try:
        # client → (sealed) → host
        await client_framer.send(json.dumps({"id": "q1", "type": "http", "method": "GET", "path": "/ping", "headers": {}, "body": ""}))
        wire = await client_to_host.get()
        assert b"http" not in wire  # encrypted on the wire
        await proxy.handle_message(host_framer.feed(wire))

        # host → (sealed) → client
        wire_back = await asyncio.wait_for(host_to_client.get(), timeout=8)
        resp = json.loads(client_framer.feed(wire_back))
        assert resp["status"] == 200, resp.get("body")
        body = json.loads(resp["body"])
        assert body["ok"] == "yes"
        assert body["via"] == "p2p"
    finally:
        await proxy.stop()
        server.should_exit = True
        thread.join(timeout=5)
