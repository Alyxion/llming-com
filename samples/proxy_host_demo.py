"""End-to-end proxy / access-hub sample (the P2P fallback transport).

Three pieces in one process:

- an **access hub** (``create_access_app``) — the public relay a browser talks to;
- a tiny **local app** — what we want to expose, on a separate port;
- a **tunnel client** — keeps one outbound WebSocket from the local app to the hub,
  so the browser reaches the app with no inbound rule on the app's network.

A browser opens the printed ``/t/{host}/{token}`` URL: the hub verifies the token
through the tunnel, sets a session cookie, and redirects to ``/proxy/{host}/`` —
the full app, proxied.

Run:

    poetry run python samples/proxy_host_demo.py
    # then open the printed URL in a browser
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from _demo_app import build_demo_app  # noqa: E402
from llming_com import create_access_app  # noqa: E402
from llming_com.access.remote import InMemoryAccessStore, TunnelClient  # noqa: E402


async def _serve(app, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


async def main() -> None:
    parser = argparse.ArgumentParser(description="llming proxy / access-hub demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--hub-port", type=int, default=8780)
    parser.add_argument("--app-port", type=int, default=8781)
    args = parser.parse_args()

    store = InMemoryAccessStore()
    store.create_user("demo", "Demo-pass123", "Demo User")
    host_entry = store.create_host("demo", "Demo Host")

    hub_app = create_access_app(store)
    local_app = build_demo_app("proxy")
    tunnel = TunnelClient(
        f"http://{args.host}:{args.hub_port}",
        host_entry.connection_key,
        local_url=f"http://{args.host}:{args.app_port}",
    )

    servers = [
        asyncio.create_task(_serve(hub_app, args.host, args.hub_port)),
        asyncio.create_task(_serve(local_app, args.host, args.app_port)),
    ]
    await asyncio.sleep(1.0)  # let both servers bind before the tunnel dials in
    tunnel_task = asyncio.create_task(tunnel.run())

    token = secrets.token_urlsafe(8)
    url = f"http://{args.host}:{args.hub_port}/t/{host_entry.host_id}/{token}"
    print("\n  llming proxy / access-hub demo")
    print(f"  host_id: {host_entry.host_id}")
    print(f"  open this in a browser:  {url}\n")

    try:
        await asyncio.gather(*servers, tunnel_task)
    finally:
        await tunnel.stop()


if __name__ == "__main__":
    asyncio.run(main())
