"""CourierClient against a live in-process server (real HTTP via urllib)."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from llming_com.courier.client import CourierClient
from llming_com.courier.config import Settings
from llming_com.courier.exceptions import NotFoundError, UnauthorizedError
from llming_com.courier.server.app import create_app

API_KEY = "live-test-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        api_keys={API_KEY},
        public_base_url=base,
        signing_key="live-signing-secret",
    )
    app = create_app(settings=settings)
    # ws="none" avoids importing the deprecated websockets.legacy module,
    # which would otherwise trip warnings-as-errors in the server thread.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for startup.
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "server failed to start"
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_encrypted_roundtrip(live_server):
    client = CourierClient(live_server, api_key=API_KEY)
    payload = b"PDF-bytes-that-must-not-touch-context" * 200
    url = client.upload(payload, content_type="application/pdf")
    assert "#k=" in url  # key rode in the fragment
    assert client.download(url) == payload


def test_plain_roundtrip_no_encryption(live_server):
    client = CourierClient(live_server, api_key=API_KEY)
    url = client.upload(b"public bytes", encrypt_payload=False)
    assert "#k=" not in url
    assert client.download(url) == b"public bytes"


def test_single_use(live_server):
    client = CourierClient(live_server, api_key=API_KEY)
    url = client.upload(b"once-only", single_use=True)
    assert client.download(url) == b"once-only"
    with pytest.raises(NotFoundError):
        client.download(url)


def test_missing_api_key_rejected(live_server):
    client = CourierClient(live_server, api_key=None)
    with pytest.raises(UnauthorizedError):
        client.upload(b"x")


def test_sha256_verification_passthrough(live_server):
    from llming_com.courier.crypto import sha256_hex

    client = CourierClient(live_server, api_key=API_KEY)
    payload = b"verify me"
    url = client.upload(payload)
    assert client.download(url, expected_sha256=sha256_hex(payload)) == payload
