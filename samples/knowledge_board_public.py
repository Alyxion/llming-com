"""Public (5G-reachable) shared knowledge board over the production hub.

Unlike ``knowledge_board_demo`` (LAN-only, runs its own hub), this connects the
host stack to the **already-deployed** hub at ``hub.openhort.ai`` so the phone
reaches a public URL from anywhere — including cellular/5G:

    PHONE (scan QR over 5G):  https://hub.openhort.ai/{owner}/{app-path}?k=<pairing>

The host runs three things locally (no inbound firewall rule needed):

- the **board app** (shared-state grid);
- a **TunnelClient** → hub  (proxy fallback transport);
- a **RelayHost** → hub ``/relay`` (P2P signaling): polls SDP offers, answers
  with a WebRTC peer using STUN+TURN ICE (fetched from the hub ``/api/ice``),
  so a direct DataChannel forms across NAT — TURN-relayed if a direct punch
  fails (symmetric NAT, most cellular).

The phone tries P2P first (direct, else TURN) and falls back to the proxy if
WebRTC can't connect at all.  The viewer reports the live path (direct / TURN /
proxy) and the board displays it.

Configuration comes from the environment (kept out of source / git):

    HUB                 hub base (default https://hub.openhort.ai)
    BOARD_ACCOUNT       owner handle (user|org|apikey)  (default llming)
    BOARD_APP           app path, may be multi-segment   (default com/samples/board)
    BOARD_ROOM          relay room id     (required)
    BOARD_CONNECTION_KEY  host tunnel key (required — proxy fallback)
    HUB_ADMISSION_KEY   relay admission key (required — P2P signaling)
    BOARD_INVITE        full public invite URL to render as the QR (required)

Run (after the prod publish step writes the KV records):

    source /path/to/.hub-board.env
    poetry run python samples/knowledge_board_public.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from knowledge_board_app import build_board_app  # noqa: E402
from llming_com import HostIdentity, RelayHost, run_secure_relay_host  # noqa: E402
from llming_com.access.remote import TunnelClient  # noqa: E402


async def _serve(app, host: str, port: int) -> None:
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).serve()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required env var {name} (source your .hub-board.env first)")
    return value


async def main() -> None:
    parser = argparse.ArgumentParser(description="public 5G-reachable knowledge board")
    parser.add_argument("--app-port", type=int, default=8791)
    args = parser.parse_args()

    hub = os.environ.get("HUB", "https://hub.openhort.ai").rstrip("/")
    # owner is a typed principal (user | org | apikey); app is a multi-segment path.
    account = os.environ.get("BOARD_ACCOUNT", "llming")           # org handle
    app_slug = os.environ.get("BOARD_APP", "com/samples/board")   # app path under the owner
    room = _require("BOARD_ROOM")
    connection_key = _require("BOARD_CONNECTION_KEY")
    admission_key = _require("HUB_ADMISSION_KEY")
    invite = _require("BOARD_INVITE")
    host_id = _require("BOARD_HOST_ID")
    identity = HostIdentity.from_pem(base64.b64decode(_require("HOST_IDENTITY_B64")).decode())
    pairing_code = _require("BOARD_PAIRING_CODE")  # the host-screen decryption key

    app_local = f"http://127.0.0.1:{args.app_port}"
    relay_endpoint = f"{hub}/relay"
    ice_endpoint = f"{hub}/api/ice"
    hub_ws = hub.replace("https://", "wss://").replace("http://", "ws://")
    # Per-room shard: this app's relay lives in its own DO isolate (keyed host:room).
    # The connection key travels in a header (passed to run_secure_relay_host), not
    # the URL, so the bare endpoint is what we connect to.
    secure_relay_url = f"{hub_ws}/securerelay/{host_id}/r/{room}/host"

    board_app = build_board_app("board", pairing_url=invite, pairing_code=pairing_code)
    notify_pairing = board_app.state.notify_pairing

    # Show the hovering pairing QR on the host when a device starts connecting,
    # hide it once that device proves it has the code. Works for P2P and relay.
    async def on_pair_request() -> None:
        await notify_pairing("requested")

    async def on_connected() -> None:
        await notify_pairing("connected")

    # Proxy fallback: outbound tunnel to the hub.
    tunnel = TunnelClient(hub, connection_key, local_url=app_local)

    # P2P signaling: answer browser offers arriving over the hub relay room,
    # using STUN+TURN minted by the hub so cellular peers can connect.
    relay_host = RelayHost(
        room,
        local_base=app_local,
        identity=identity,
        code=pairing_code,  # P2P is now code-bound too — same host-screen code
        on_pair_request=on_pair_request,
        on_connected=on_connected,
        relay_endpoint=relay_endpoint,
        admission_key=admission_key,
        ice_endpoint=ice_endpoint,
        stun_servers=["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"],
        app_id=f"{account}/{app_slug}",
        app_name="Shared Knowledge Board",
    )

    server_task = asyncio.create_task(_serve(board_app, "127.0.0.1", args.app_port))
    await asyncio.sleep(1.0)
    tunnel_task = asyncio.create_task(tunnel.run())
    await relay_host.start()
    # E2E-encrypted blind-relay fallback: the hub forwards only ciphertext.
    secure_task = asyncio.create_task(
        run_secure_relay_host(
            secure_relay_url, identity=identity, local_base=app_local, code=pairing_code,
            connection_key=connection_key,  # sent as a header, not in the URL
            on_pair_request=on_pair_request, on_connected=on_connected,
        )
    )

    host_view = f"http://127.0.0.1:{args.app_port}/?role=host&qr=1"
    stable = f"{hub}/{account}/{app_slug}"
    print("\n  🧠 Shared Knowledge Board — PUBLIC (5G-reachable)")
    print(f"  HOST  (open on this machine): {host_view}")
    print(f"  PHONE (scan QR over 5G):      {stable}?k=…  (rendered on the host page)")
    print(f"  stable URL (reconnect later): {stable}")
    print(f"  relay room:                   {room}  via {relay_endpoint}")
    print(f"  host key fingerprint:         {identity.fingerprint}")
    print("  Path shown in UI: P2P · direct | P2P · TURN relay | proxy · E2E encrypted\n")

    try:
        await asyncio.gather(server_task, tunnel_task, secure_task)
    finally:
        await relay_host.stop()
        await tunnel.stop()


if __name__ == "__main__":
    asyncio.run(main())
