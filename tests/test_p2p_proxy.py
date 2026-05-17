"""Tests for the generic DataChannel proxy."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llming_com.p2p.proxy import DataChannelProxy, OneTimeTokenStore, ReconnectTokenStore


class FakePeer:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_datachannel_http_request_forwards_to_local_app() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["forwarded_via"] = request.headers.get("x-forwarded-via")
        seen["body"] = request.content
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True})

    peer = FakePeer()
    proxy = DataChannelProxy(peer, local_base="http://internal")
    proxy._http_client = httpx.AsyncClient(
        base_url="http://internal",
        transport=httpx.MockTransport(handler),
    )

    await proxy.handle_message(json.dumps({
        "id": "r1",
        "type": "http",
        "method": "POST",
        "path": "/api/do?x=1",
        "headers": {"host": "public.example"},
        "body": "payload",
    }))
    await asyncio.sleep(0)

    assert seen == {
        "method": "POST",
        "url": "http://internal/api/do?x=1",
        "forwarded_via": "p2p",
        "body": b"payload",
    }
    response = json.loads(peer.sent[-1])
    assert response["id"] == "r1"
    assert response["type"] == "http_response"
    assert response["status"] == 200
    assert response["body"] == '{"ok":true}'
    assert response["binary"] is False


def test_one_time_tokens_expire_and_consume() -> None:
    store = OneTimeTokenStore(ttl=60)
    token = store.generate()
    assert store.pending_count == 1
    assert store.verify(token) is True
    assert store.verify(token) is False


@pytest.mark.asyncio
async def test_ping_issues_refreshable_reconnect_token() -> None:
    peer = FakePeer()
    proxy = DataChannelProxy(peer)
    reconnect_store = ReconnectTokenStore()
    proxy.attach_reconnect_store(reconnect_store)

    await proxy.handle_message(json.dumps({"type": "ping", "ts": 123}))
    pong = json.loads(peer.sent[-1])
    token = pong["reconnect_token"]
    assert pong["type"] == "pong"
    assert reconnect_store.verify(token)

    await proxy.handle_message(json.dumps({"type": "ping", "ts": 456}))
    second = json.loads(peer.sent[-1])
    assert second["reconnect_token"] == token
    assert reconnect_store.verify(token)
