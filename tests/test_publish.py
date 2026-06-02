"""Tests for named app publishing: registry, lifetime, device grants, routes."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from llming_com import (
    PublishRegistry,
    create_access_app,
    mount_p2p_host,
    mount_publish,
    serve_published,
)
from llming_com.access.remote import InMemoryAccessStore
from llming_com.publish import normalize_slug

# ---- registry ----


def test_normalize_slug() -> None:
    assert normalize_slug("Acme") == "acme"
    assert normalize_slug("my-app-1") == "my-app-1"
    for bad in ["", "-x", "a/b", "white space", "x" * 64]:
        with pytest.raises(ValueError):
            normalize_slug(bad)


def test_publish_resolve_and_expiry() -> None:
    reg = PublishRegistry()
    reg.publish("acme", "dash", ttl_seconds=0)  # no expiry
    assert reg.resolve("acme", "dash") is not None
    assert reg.resolve("ACME", "dash") is not None  # case-insensitive

    reg.publish("acme", "short", ttl_seconds=1000)
    future = time.time() + 2000
    assert reg.resolve("acme", "short", now=future) is None  # expired in the future


def test_pairing_redeem_is_one_time_and_yields_device() -> None:
    reg = PublishRegistry()
    reg.publish("acme", "dash")
    token = reg.issue_pairing("acme", "dash")
    record, cred = reg.redeem_pairing(token)
    assert record.slug == "acme/dash"
    assert reg.verify_device(cred) is not None
    with pytest.raises(KeyError):
        reg.redeem_pairing(token)  # consumed


def test_device_grant_bounded_by_link_lifetime() -> None:
    reg = PublishRegistry()
    reg.publish("acme", "dash", ttl_seconds=1000)
    _, cred = reg.redeem_pairing(reg.issue_pairing("acme", "dash"))
    assert reg.verify_device(cred) is not None
    assert reg.verify_device(cred, now=time.time() + 2000) is None  # link expired -> grant inactive


def test_revoke_drops_app_and_devices() -> None:
    reg = PublishRegistry()
    reg.publish("acme", "dash")
    _, cred = reg.redeem_pairing(reg.issue_pairing("acme", "dash"))
    reg.revoke("acme", "dash")
    assert reg.resolve("acme", "dash") is None
    assert reg.verify_device(cred) is None


def test_issue_pairing_unknown_app() -> None:
    reg = PublishRegistry()
    with pytest.raises(KeyError):
        reg.issue_pairing("acme", "ghost")


# ---- mounted routes ----


def _hub_with_publish(ttl: float = 0.0):
    store = InMemoryAccessStore()
    store.create_user("demo", "Demo-pass123")
    host = store.create_host("demo", "Host")
    app = create_access_app(store)
    mount_p2p_host(app, local_base="http://127.0.0.1:9999", mode="p2p")
    reg = PublishRegistry()
    reg.publish("acme", "dash", modes=["p2p+proxy"], host_id=host.host_id, ttl_seconds=ttl)
    mount_publish(app, registry=reg, hub_base="", p2p_offer_url="/p2p/offer", p2p_stun=[])
    return app, reg


def test_launcher_injects_config_block() -> None:
    app, _ = _hub_with_publish()
    body = TestClient(app).get("/acme/dash").text
    assert 'id="llming-p2p-config"' in body
    assert "acme" in body and "handshake" in body


def test_reserved_account_not_shadowed() -> None:
    app, _ = _hub_with_publish()
    client = TestClient(app)
    assert client.get("/p2p/config").status_code == 200       # real p2p route wins
    assert client.get("/p2p/viewer.html").status_code == 200   # raw viewer still served


def test_redeem_then_handshake_descriptor() -> None:
    app, reg = _hub_with_publish()
    client = TestClient(app)
    token = reg.issue_pairing("acme", "dash")
    cred = client.post("/acme/dash/api/pair/redeem", json={"pairing_token": token}).json()["device_credential"]
    desc = client.post("/acme/dash/api/handshake", json={"device_credential": cred}).json()
    assert desc["mode"] == "p2p+proxy"
    assert desc["signal"] == "http"
    assert desc["proxy_fallback_url"].startswith("/t/")


def test_bad_credential_and_token() -> None:
    app, _ = _hub_with_publish()
    client = TestClient(app)
    assert client.post("/acme/dash/api/pair/redeem", json={"pairing_token": "nope"}).status_code == 401
    assert client.post("/acme/dash/api/handshake", json={"device_credential": "nope"}).status_code == 401


def test_expired_link_blocks_launcher_and_handshake() -> None:
    store = InMemoryAccessStore()
    store.create_user("demo", "Demo-pass123")
    host = store.create_host("demo", "Host")
    app = create_access_app(store)
    mount_p2p_host(app, local_base="http://127.0.0.1:9999", mode="p2p")
    reg = PublishRegistry()
    reg.publish("acme", "dash", modes=["p2p+proxy"], host_id=host.host_id, ttl_seconds=1000)
    # mint a credential while live, then expire the link
    _, cred = reg.redeem_pairing(reg.issue_pairing("acme", "dash"))
    reg.revoke("acme", "dash")  # simulate expiry/teardown
    mount_publish(app, registry=reg, hub_base="", p2p_offer_url="/p2p/offer", p2p_stun=[])
    client = TestClient(app)
    assert client.get("/acme/dash").status_code == 404
    assert client.post("/acme/dash/api/handshake", json={"device_credential": cred}).status_code == 410


def test_serve_published_helper() -> None:
    from fastapi import FastAPI
    from starlette.responses import HTMLResponse

    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse("<html><head></head><body>hi</body></html>")

    registry, pairing_url = serve_published(app, account="acme", app_name="dash", port=8801)
    assert "/acme/dash?k=" in pairing_url
    assert registry.resolve("acme", "dash") is not None
    # the publish + p2p routes are now on the app
    body = TestClient(app).get("/acme/dash").text
    assert 'id="llming-p2p-config"' in body
