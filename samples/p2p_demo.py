"""End-to-end P2P sample.

One FastAPI server hosts a tiny demo app *and* the P2P signaling endpoints.
A browser opens the generic viewer, establishes a direct WebRTC DataChannel back
to this server, and loads the app entirely over that channel — no relay, no
inbound firewall rule.

Run:

    poetry run python samples/p2p_demo.py
    # then open the printed viewer URL in a browser

Enable proxy fallback (mode "p2p+proxy") by also running the proxy sample and
passing its token URL:

    poetry run python samples/p2p_demo.py --proxy-fallback "http://127.0.0.1:8780/t/<host>/<tok>"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from _demo_app import build_demo_app  # noqa: E402
from llming_com import mount_p2p_host  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="llming P2P end-to-end demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--proxy-fallback", default="", help="proxy hub URL used when P2P fails")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    mode = "p2p+proxy" if args.proxy_fallback else "p2p"

    app = build_demo_app("p2p")
    mount_p2p_host(app, local_base=base, mode=mode, proxy_fallback_url=args.proxy_fallback)

    print("\n  llming P2P demo")
    print(f"  mode: {mode}")
    print(f"  open this in a browser:  {base}/p2p/viewer.html\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
