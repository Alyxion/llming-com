"""End-to-end *named publishing* sample: stable URL, pairing, durable reconnect.

Pieces (one process):

- a tiny **shared app** (``_demo_app``) on its own port;
- a **hub** = access proxy hub + P2P signaling host + publish registry;
- a **tunnel client** exposing the app to the hub for the proxy transport.

The app is published at a stable URL:

    http://127.0.0.1:8790/acme/dashboard

A device pairs ONCE via the printed invite link (``?k=...``, or render it as a
QR).  The browser stores a durable credential and lands on the stable URL.
Reload that URL after powersave / 10 minutes / the next morning and it
re-handshakes and reconnects — no re-scan — until the published link expires.

Connectivity is ``p2p+proxy``: a direct WebRTC DataChannel first, automatic
fallback to the proxy hub if P2P can't be established.

Run:

    poetry run python samples/publish_demo.py
    # open the printed invite link once, then reload the stable URL to reconnect
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from _demo_app import build_demo_app  # noqa: E402
from llming_com import create_access_app, mount_p2p_host, mount_publish  # noqa: E402
from llming_com.access.remote import InMemoryAccessStore, TunnelClient  # noqa: E402
from llming_com.publish import PublishRegistry  # noqa: E402


async def _serve(app, host: str, port: int) -> None:
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).serve()


async def main() -> None:
    parser = argparse.ArgumentParser(description="llming named-publishing demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--hub-port", type=int, default=8790)
    parser.add_argument("--app-port", type=int, default=8791)
    parser.add_argument("--account", default="acme")
    parser.add_argument("--app", default="dashboard")
    parser.add_argument("--ttl", type=float, default=3600.0, help="published link lifetime (s); 0 = no expiry")
    args = parser.parse_args()

    hub_base = f"http://{args.host}:{args.hub_port}"
    app_base = f"http://{args.host}:{args.app_port}"

    # access hub (proxy transport) + a registered host for the shared app
    store = InMemoryAccessStore()
    store.create_user("demo", "Demo-pass123", "Demo User")
    host_entry = store.create_host("demo", "Demo Host")
    hub_app = create_access_app(store)

    # P2P signaling host — forwards the DataChannel to the shared app
    mount_p2p_host(hub_app, local_base=app_base, mode="p2p")

    # publish the app under a stable name, mode p2p with proxy fallback
    registry = PublishRegistry()
    registry.publish(
        args.account, args.app,
        modes=["p2p+proxy"],
        host_id=host_entry.host_id,
        display_name="Acme Dashboard",
        ttl_seconds=args.ttl,
    )
    mount_publish(hub_app, registry=registry, hub_base=hub_base, p2p_offer_url="/p2p/offer", p2p_stun=[])

    shared_app = build_demo_app("published")
    tunnel = TunnelClient(hub_base, host_entry.connection_key, local_url=app_base)

    servers = [
        asyncio.create_task(_serve(hub_app, args.host, args.hub_port)),
        asyncio.create_task(_serve(shared_app, args.host, args.app_port)),
    ]
    await asyncio.sleep(1.0)
    tunnel_task = asyncio.create_task(tunnel.run())

    pairing = registry.issue_pairing(args.account, args.app)
    stable = f"{hub_base}/{args.account}/{args.app}"
    print("\n  llming named-publishing demo")
    print(f"  stable URL (bookmark this): {stable}")
    print(f"  invite link (open ONCE):    {stable}?k={pairing}")
    print(f"  link lifetime:              {'no expiry' if not args.ttl else str(int(args.ttl)) + 's'}")
    print("  after pairing, reload the stable URL anytime to reconnect.\n")

    try:
        await asyncio.gather(*servers, tunnel_task)
    finally:
        await tunnel.stop()


if __name__ == "__main__":
    asyncio.run(main())
