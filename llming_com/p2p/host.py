"""Self-contained P2P host: HTTP signaling + transparent DataChannel tunnel.

This is the FastAPI deployment backend for the shared P2P transport.  It lets a
host expose one local app to a browser over a direct WebRTC DataChannel, using
plain HTTP for the one-shot SDP exchange (no relay, no inbound firewall rule on
LAN/localhost).  The same browser viewer also works against the Cloudflare relay
backend for remote NAT traversal; see ``llming_com/server/p2p/relay``.

Connectivity modes (surfaced to the viewer via ``GET {prefix}/config``):

- ``"p2p"``    — direct WebRTC only.
- ``"proxy"``  — skip P2P, go straight to ``proxy_fallback_url``.
- ``"p2p+proxy"`` — try P2P first, fall back to the proxy hub if it fails.

The proxy fallback is the access hub from :mod:`llming_com.access.remote`.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from llming_com.p2p.proxy import DataChannelProxy
from llming_com.p2p.webrtc import WebRTCPeerRegistry
from llming_com.publish import PublishRegistry

logger = logging.getLogger(__name__)

VIEWER_DIR = Path(__file__).resolve().parent.parent / "static" / "p2p"

VALID_MODES = {"p2p", "proxy", "p2p+proxy"}

# First path segment values that belong to other route families, never an account.
RESERVED_ACCOUNTS = {
    "p2p", "proxy", "api", "t", "static", "_llming", "ws", "assets",
    "favicon.ico", "health", "robots.txt",
}


def _viewer_html() -> str:
    return (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def mount_p2p_host(
    app: FastAPI,
    *,
    local_base: str,
    prefix: str = "/p2p",
    mode: str = "p2p+proxy",
    proxy_fallback_url: str = "",
    stun_servers: list[str] | None = None,
    p2p_connect_timeout_ms: int = 15000,
) -> WebRTCPeerRegistry:
    """Mount the P2P signaling + viewer routes onto an existing FastAPI app.

    ``local_base`` is the origin the DataChannel proxy forwards browser requests
    to — usually the same server that hosts the app being shared.
    """

    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    if mode != "p2p" and not proxy_fallback_url:
        logger.warning("mode %r requested without proxy_fallback_url; falling back to pure p2p", mode)
        mode = "p2p"

    # Resolve one ICE config used by both the server peer and the browser.
    # Localhost/LAN needs no STUN: host-candidate-only ICE completes instantly.
    if stun_servers is not None:
        effective_stun = stun_servers
    elif local_base.startswith(("http://127.", "http://localhost", "http://0.0.0.0")):
        effective_stun = []
    else:
        effective_stun = ["stun:stun.l.google.com:19302"]

    registry = WebRTCPeerRegistry()
    prefix = prefix.rstrip("/")

    @app.get(f"{prefix}/config")
    async def p2p_config() -> Response:
        return JSONResponse({
            "signal": "http",
            "mode": mode,
            "offer_url": f"{prefix}/offer",
            "proxy_fallback_url": proxy_fallback_url,
            "p2p_connect_timeout_ms": p2p_connect_timeout_ms,
            # Advertise the host's ICE config so the browser matches it. An empty
            # list means host-candidate-only (LAN/localhost) — fastest handshake.
            "stun_servers": effective_stun,
        })

    @app.post(f"{prefix}/offer")
    async def p2p_offer(request: Request) -> Response:
        body = await request.json()
        offer_sdp = body.get("sdp", "")
        if not offer_sdp:
            return JSONResponse({"error": "missing sdp"}, status_code=400)
        import secrets

        session_id = body.get("session_id") or secrets.token_hex(8)

        proxy_holder: dict[str, DataChannelProxy] = {}

        async def on_message(message: bytes | str) -> None:
            proxy = proxy_holder.get("proxy")
            if proxy is not None:
                await proxy.handle_message(message)

        async def on_open() -> None:
            proxy = proxy_holder.get("proxy")
            if proxy is not None:
                await proxy.start()

        peer, answer_sdp = await registry.create_peer(
            session_id,
            offer_sdp,
            on_message=on_message,
            on_open=on_open,
            stun_servers=effective_stun,
        )
        proxy_holder["proxy"] = DataChannelProxy(peer, local_base=local_base)
        return JSONResponse({"type": "answer", "sdp": answer_sdp, "session_id": session_id})

    @app.get(f"{prefix}/status")
    async def p2p_status() -> Response:
        return JSONResponse({"active_sessions": registry.active_sessions, "peers": len(registry)})

    @app.get(f"{prefix}/viewer.html", response_class=HTMLResponse)
    @app.get(f"{prefix}/", response_class=HTMLResponse)
    async def p2p_viewer() -> Response:
        return HTMLResponse(_viewer_html())

    @app.on_event("shutdown")
    async def _close_peers() -> None:
        await registry.close_all()

    app.state.llming_p2p_registry = registry
    return registry


def mount_publish(
    app: FastAPI,
    *,
    registry: PublishRegistry,
    hub_base: str = "",
    p2p_offer_url: str = "/p2p/offer",
    p2p_stun: list[str] | None = None,
) -> None:
    """Mount named-app publishing: stable ``/{account}/{app}`` URLs.

    The launcher served at ``/{account}/{app}`` is the same generic viewer, with
    a publish config block injected.  It redeems a pairing/share token once into
    a durable device credential, then handshakes on every load for a fresh
    connection — so reload-after-powersave reconnects with no re-scan.

    It reuses the existing transports unchanged: ``/p2p/offer`` for P2P and the
    backward-compatible ``/t/{host}/{token}`` token login for the proxy hub.
    Call this AFTER the access-hub and p2p-host routes are mounted so their
    specific routes take precedence over the ``/{account}/{app}`` catch-all.
    """

    hub_base = hub_base.rstrip("/")

    def _descriptor(record) -> dict:  # type: ignore[no-untyped-def]
        desc: dict[str, object] = {"mode": record.mode}
        if "p2p" in record.mode:
            if record.relay_endpoint and record.room:
                desc.update(signal="ws", relay=record.relay_endpoint, room=record.room)
            else:
                desc.update(signal="http", offer_url=p2p_offer_url, stun_servers=p2p_stun or [])
        if record.host_id and "proxy" in record.mode:
            # One-time token-login URL — sets the session cookie on the hub origin
            # and redirects to /proxy/{host}/ (the existing, backward-compatible path).
            token = secrets.token_urlsafe(8)
            desc["proxy_fallback_url"] = f"{hub_base}/t/{record.host_id}/{token}"
        return desc

    def _config_block(record) -> dict:  # type: ignore[no-untyped-def]
        return {
            "publish": {
                "account": record.account,
                "app": record.app,
                "display_name": record.display_name,
            },
            "redeem_url": f"/{record.account}/{record.app}/api/pair/redeem",
            "handshake_url": f"/{record.account}/{record.app}/api/handshake",
            "mode": record.mode,
        }

    @app.get("/{account}/{app_name}", response_class=HTMLResponse)
    @app.get("/{account}/{app_name}/", response_class=HTMLResponse)
    async def publish_launcher(account: str, app_name: str) -> Response:
        if account in RESERVED_ACCOUNTS:
            raise HTTPException(404, "not found")
        record = registry.resolve(account, app_name)
        if record is None:
            raise HTTPException(404, "no such app, or the link has expired")
        block = (
            '<script id="llming-p2p-config" type="application/json">'
            + json.dumps(_config_block(record))
            + "</script>"
        )
        return HTMLResponse(_viewer_html().replace("<!--LLMING_P2P_CONFIG-->", block, 1))

    @app.get("/{account}/{app_name}/config")
    async def publish_config(account: str, app_name: str) -> Response:
        if account in RESERVED_ACCOUNTS:
            raise HTTPException(404, "not found")
        record = registry.resolve(account, app_name)
        if record is None:
            raise HTTPException(404, "no such app, or the link has expired")
        return JSONResponse(_config_block(record))

    @app.post("/{account}/{app_name}/api/pair/redeem")
    async def publish_redeem(account: str, app_name: str, request: Request) -> Response:
        body = await request.json()
        try:
            record, credential = registry.redeem_pairing(body.get("pairing_token", ""))
        except KeyError:
            raise HTTPException(401, "invalid or expired pairing token")
        if record.account != account or record.app != app_name:
            raise HTTPException(400, "token does not match this app")
        return JSONResponse({"device_credential": credential, "account": record.account, "app": record.app})

    @app.post("/{account}/{app_name}/api/handshake")
    async def publish_handshake(account: str, app_name: str, request: Request) -> Response:
        record = registry.resolve(account, app_name)
        if record is None:
            raise HTTPException(410, "link expired")
        body = await request.json()
        grant = registry.verify_device(body.get("device_credential", ""))
        if grant is None or grant.account != record.account or grant.app != record.app:
            raise HTTPException(401, "device not paired")
        return JSONResponse(_descriptor(record))


def serve_published(
    app: FastAPI,
    *,
    account: str,
    app_name: str,
    host: str = "127.0.0.1",
    port: int = 8800,
    ttl_seconds: float = 0.0,
    display_name: str = "",
) -> tuple[PublishRegistry, str]:
    """One-call P2P publishing for newcomers.

    Adds P2P signaling + a named, durable, bookmarkable URL in front of an
    existing FastAPI ``app`` and returns ``(registry, pairing_url)``.  The app is
    reachable at ``http://{host}:{port}/{account}/{app_name}``; share the
    returned pairing URL (or render it as a QR) to let a device pair once and
    reconnect forever after (until ``ttl_seconds`` elapses, 0 = no expiry).

    Run the returned app yourself, e.g. ``uvicorn.run(app, host=host, port=port)``.
    """

    base = f"http://{host}:{port}"
    registry = PublishRegistry()
    record = registry.publish(
        account, app_name, modes=["p2p"], ttl_seconds=ttl_seconds, display_name=display_name
    )
    mount_p2p_host(app, local_base=base, mode="p2p")
    mount_publish(app, registry=registry, hub_base=base, p2p_offer_url="/p2p/offer", p2p_stun=[])
    pairing = registry.issue_pairing(record.account, record.app)
    return registry, f"{base}/{record.account}/{record.app}?k={pairing}"


def create_p2p_host_app(
    local_base: str,
    *,
    mode: str = "p2p",
    proxy_fallback_url: str = "",
    stun_servers: list[str] | None = None,
) -> FastAPI:
    """Create a standalone FastAPI app that fronts ``local_base`` over P2P.

    Useful when the shared app runs as a separate process; for most samples the
    app and the P2P host are the same server, so :func:`mount_p2p_host` is used.
    """

    app = FastAPI(title="llming-p2p-host")
    mount_p2p_host(
        app,
        local_base=local_base,
        mode=mode,
        proxy_fallback_url=proxy_fallback_url,
        stun_servers=stun_servers,
    )
    return app
