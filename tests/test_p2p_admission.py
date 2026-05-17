"""Tests for deployment-neutral relay admission client."""

from __future__ import annotations

import json
from typing import Any

import pytest

from llming_com.p2p.admission import P2PAdmissionClient, P2PAdmissionError


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


@pytest.mark.asyncio
async def test_register_room_uses_bearer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: int) -> _FakeResponse:
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"room": "room-1", "expires_at": 123, "config": {"poll_interval_ms": 5000}})

    monkeypatch.setattr("llming_com.p2p.admission.request.urlopen", fake_urlopen)

    client = P2PAdmissionClient("https://relay.example.com", "secret")
    grant = await client.register_room("room-1", app_id="app", app_name="App", ttl_ms=60_000)

    assert seen == {
        "url": "https://relay.example.com/room-1/register",
        "timeout": 15,
        "auth": "Bearer secret",
        "body": {"app_id": "app", "app_name": "App", "ttl_ms": 60_000},
    }
    assert grant.room == "room-1"
    assert grant.expires_at == 123
    assert grant.config == {"poll_interval_ms": 5000}


def test_room_urls_support_hub_prefix() -> None:
    client = P2PAdmissionClient("https://hub.openhort.ai/relay", "secret")

    assert client.room_http_url("room 1", "pending") == "https://hub.openhort.ai/relay/room%201/pending"
    assert client.room_ws_url("room 1") == "wss://hub.openhort.ai/relay/room%201"


def test_pairing_url_keeps_token_in_fragment() -> None:
    client = P2PAdmissionClient("https://relay.example.com", "secret")

    assert (
        client.pairing_url("https://example.com/p2p/pair", "abc+123")
        == "https://example.com/p2p/pair#pt=abc%2B123"
    )


@pytest.mark.asyncio
async def test_authorized_request_requires_key() -> None:
    client = P2PAdmissionClient("https://relay.example.com")

    with pytest.raises(P2PAdmissionError, match="missing relay admission key"):
        await client.register_room("room-1")
