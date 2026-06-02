"""QR / smartphone demo: a *shared knowledge board* over P2P (proxy fallback).

One process wires together everything we built:

- the **board app** (``knowledge_board_app``) — a grid of tiles whose on/off
  state is shared live over one WebSocket across every connected client;
- a **hub** = access proxy + P2P signaling host + publish registry;
- a **tunnel client** exposing the board to the hub for the proxy transport.

The board is published at a stable URL and bound to the machine's LAN IP so a
phone on the same Wi-Fi can reach it:

    HOST  (this screen):  http://127.0.0.1:8791/?role=host&qr=1
    PHONE (scan the QR):  http://<LAN-IP>:8790/team/board?k=<pairing>

Open the HOST url on this machine — it shows the board **and a QR code**.
Scan the QR with your phone: it pairs once (durable credential), connects over
a direct WebRTC DataChannel, and shows the same board.  **Tap a tile on either
device and it lights up on both** — shared knowledge over the P2P session.

Run:

    poetry run python samples/knowledge_board_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from knowledge_board_app import build_board_app  # noqa: E402
from llming_com import create_access_app, mount_p2p_host, mount_publish  # noqa: E402
from llming_com.access.remote import InMemoryAccessStore, TunnelClient  # noqa: E402
from llming_com.publish import PublishRegistry  # noqa: E402


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 (no traffic actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def _serve(app, host: str, port: int) -> None:
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).serve()


async def main() -> None:
    parser = argparse.ArgumentParser(description="shared knowledge board QR demo")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address (0.0.0.0 so phones can reach)")
    parser.add_argument("--ip", default="", help="LAN IP advertised in the QR (auto-detected if empty)")
    parser.add_argument("--hub-port", type=int, default=8790)
    parser.add_argument("--app-port", type=int, default=8791)
    parser.add_argument("--account", default="team")
    parser.add_argument("--app", default="board")
    parser.add_argument("--ttl", type=float, default=3600.0, help="published link lifetime (s); 0 = no expiry")
    args = parser.parse_args()

    ip = args.ip or lan_ip()
    hub_base = f"http://{ip}:{args.hub_port}"            # reachable from the phone
    app_local = f"http://127.0.0.1:{args.app_port}"      # internal: hub → board app

    # access hub (proxy transport) + a registered host for the board
    store = InMemoryAccessStore()
    store.create_user("demo", "Demo-pass123", "Demo User")
    host_entry = store.create_host("demo", "Board Host")
    hub_app = create_access_app(store)

    # P2P signaling host — forwards the DataChannel to the board app.
    # Host-only ICE (no STUN) is enough on a LAN and keeps handshakes instant.
    mount_p2p_host(hub_app, local_base=app_local, mode="p2p", stun_servers=[])

    # publish the board under a stable name: p2p first, proxy fallback
    registry = PublishRegistry()
    registry.publish(
        args.account, args.app,
        modes=["p2p+proxy"],
        host_id=host_entry.host_id,
        display_name="Shared Knowledge Board",
        ttl_seconds=args.ttl,
    )
    mount_publish(hub_app, registry=registry, hub_base=hub_base, p2p_offer_url="/p2p/offer", p2p_stun=[])

    pairing = registry.issue_pairing(args.account, args.app)
    stable = f"{hub_base}/{args.account}/{args.app}"
    invite = f"{stable}?k={pairing}"

    # board app — host view renders the QR pointing at the phone invite link
    board_app = build_board_app("board", pairing_url=invite)
    tunnel = TunnelClient(hub_base, host_entry.connection_key, local_url=app_local)

    servers = [
        asyncio.create_task(_serve(hub_app, args.bind, args.hub_port)),
        asyncio.create_task(_serve(board_app, args.bind, args.app_port)),
    ]
    await asyncio.sleep(1.0)
    tunnel_task = asyncio.create_task(tunnel.run())

    host_view = f"http://127.0.0.1:{args.app_port}/?role=host&qr=1"
    print("\n  🧠 Shared Knowledge Board — QR demo")
    print(f"  HOST  (open on this machine): {host_view}")
    print(f"  PHONE (scan the QR, or open): {invite}")
    print(f"  stable URL (reconnect later): {stable}")
    print(f"  link lifetime:                {'no expiry' if not args.ttl else str(int(args.ttl)) + 's'}")
    print("  Tap a tile on either device — it lights up on both.\n")

    try:
        await asyncio.gather(*servers, tunnel_task)
    finally:
        await tunnel.stop()


if __name__ == "__main__":
    asyncio.run(main())
