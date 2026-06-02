"""Shared tiny app used by the P2P and proxy samples.

The same app is reachable two ways:

- directly over a WebRTC DataChannel (P2P sample), where it loads at origin ``/``;
- through the access hub (proxy sample), where it loads under ``/proxy/{host}/``
  and the hub injects a ``<base>`` tag.

So the page is transport-agnostic: it derives a base prefix from the ``<base>``
tag and builds every HTTP and WebSocket URL relative to it.  ``/api/info`` echoes
the ``x-forwarded-via`` header so the page can show whether it arrived over
``p2p`` or ``proxy``.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import HTMLResponse, JSONResponse

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llming demo app — {label}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:40px; }}
  .card {{ max-width:640px; margin:0 auto; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:28px; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; background:#1f6feb33; color:#79c0ff; }}
  .row {{ margin-top:18px; padding:12px 14px; background:#0d1117; border:1px solid #21262d; border-radius:8px; font-family:ui-monospace,monospace; font-size:13px; }}
  .k {{ color:#7d8590; }}
  #transport {{ color:#4ade80; font-weight:700; }}
</style>
</head>
<body>
<div class="card">
  <h1>🛰️ llming demo app <span class="badge">{label}</span></h1>
  <p>Delivered over the <span id="transport">…</span> transport.</p>
  <div class="row"><span class="k">GET api/info →</span> <span id="info">loading…</span></div>
  <div class="row"><span class="k">WS ws/echo →</span> <span id="ws">connecting…</span></div>
</div>
<script>
(async () => {{
  const baseHref = (document.querySelector('base') || {{}}).getAttribute ? document.querySelector('base').getAttribute('href') : null;
  const base = baseHref || '/';
  try {{
    const res = await fetch(base + 'api/info', {{ cache: 'no-store' }});
    const data = await res.json();
    document.getElementById('transport').textContent = data.forwarded_via || 'direct';
    document.getElementById('info').textContent = JSON.stringify(data);
  }} catch (e) {{ document.getElementById('info').textContent = 'error: ' + e; }}
  try {{
    const u = new URL(base + 'ws/echo', location.href);
    u.protocol = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    const ws = new WebSocket(u.href);
    ws.onopen = () => ws.send('hello-over-tunnel');
    ws.onmessage = (ev) => {{ document.getElementById('ws').textContent = ev.data; }};
    ws.onerror = () => {{ document.getElementById('ws').textContent = 'ws error'; }};
  }} catch (e) {{ document.getElementById('ws').textContent = 'error: ' + e; }}
}})();
</script>
</body>
</html>
"""


def build_demo_app(label: str = "app") -> FastAPI:
    app = FastAPI(title=f"llming-demo-{label}")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(PAGE.format(label=label))

    @app.get("/api/info")
    async def info(request: Request) -> JSONResponse:
        return JSONResponse({
            "app": label,
            "forwarded_via": request.headers.get("x-forwarded-via", "direct"),
            "ok": True,
        })

    @app.websocket("/ws/echo")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                msg = await websocket.receive_text()
                await websocket.send_text(f"echo: {msg}")
        except WebSocketDisconnect:
            return

    # Used by the access hub token-login flow (/t/{host}/{token}); the demo
    # accepts any token so the sample is one click. Real hosts validate here.
    @app.post("/_llming/remote/verify-token")
    async def verify_token() -> JSONResponse:
        return JSONResponse({"valid": True})

    return app
