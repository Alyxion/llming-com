"""Shared *knowledge board* app for the QR / smartphone demo.

A small transport-agnostic page that renders a grid of tiles whose state is
**shared live across every connected client** over one WebSocket
(``ws/board``).  Tap a tile on your phone and it lights up instantly on the
host screen too — and vice-versa.  This is the "shared knowledge, a click
affects both sides" demo.

The same app is reachable two ways, exactly like ``_demo_app``:

- directly on the host (loads at origin ``/``);
- through a published P2P/proxy URL on the phone (loads under an injected
  ``<base>`` prefix and tunnels the WebSocket transparently).

So every HTTP/WS URL is built relative to the ``<base>`` tag.  ``/api/info``
echoes ``x-forwarded-via`` so each client can show whether it arrived over
``p2p`` or ``proxy``.  ``/qr.svg`` renders a pairing QR (host view only).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import HTMLResponse, JSONResponse, Response

GRID = 9  # 3x3 board

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shared Knowledge Board</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: system-ui, sans-serif; background:#0d1117; color:#e6edf3;
         margin:0; padding:24px; -webkit-tap-highlight-color: transparent; }}
  .wrap {{ max-width:560px; margin:0 auto; }}
  h1 {{ margin:0 0 4px; font-size:20px; }}
  .sub {{ color:#7d8590; font-size:13px; margin-bottom:18px; }}
  .who {{ color:#79c0ff; font-weight:700; }}
  #transport {{ color:#4ade80; font-weight:700; }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
  .tile {{ aspect-ratio:1/1; border-radius:16px; border:1px solid #30363d;
          background:#161b22; cursor:pointer; display:flex; align-items:center;
          justify-content:center; font-size:34px; font-weight:800; color:#30363d;
          user-select:none; transition:transform .06s ease, background .15s ease, color .15s ease; }}
  .tile:active {{ transform:scale(.94); }}
  .tile.on {{ background:#1f6feb; color:#fff; border-color:#1f6feb;
             box-shadow:0 0 0 3px #1f6feb44; }}
  .bar {{ margin-top:18px; padding:10px 14px; background:#161b22; border:1px solid #21262d;
         border-radius:10px; font-size:13px; display:flex; justify-content:space-between; }}
  .qr {{ text-align:center; margin:18px 0; }}
  /* Round the CONTAINER, not the <svg> — a border-radius on the svg itself
     clips the QR's corner finder patterns and breaks scanning. */
  .qr #qrbox {{ display:inline-block; background:#fff; padding:14px; border-radius:14px; line-height:0; }}
  /* Render at the SVG's natural size (robust whether or not segno emits a
     viewBox) and just cap it — avoids the scaling clip a fixed px size causes. */
  .qr svg {{ display:block; width:240px; height:240px; }}
  .qr .hint {{ color:#7d8590; font-size:13px; margin-top:8px; }}
  .pulse {{ animation:flash .5s ease; }}
  @keyframes flash {{ 0%{{box-shadow:0 0 0 6px #4ade8088;}} 100%{{box-shadow:none;}} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>🧠 Shared Knowledge Board</h1>
  <div class="sub">role: <b id="role">{role}</b> · transport: <span id="transport">…</span> ·
       <span id="peers">0</span> connected</div>
  {qr_block}
  <div class="grid" id="grid"></div>
  <div class="bar">
    <span>last change by <span class="who" id="lastby">—</span></span>
    <span id="status">connecting…</span>
  </div>
</div>
<script>
(function() {{
  var baseEl = document.querySelector('base');
  var base = (baseEl && baseEl.getAttribute('href')) || '/';
  var ROLE = {role_json};
  var N = {grid};
  var grid = document.getElementById('grid');
  var tiles = [];
  for (var i = 0; i < N; i++) {{
    (function(idx) {{
      var t = document.createElement('div');
      t.className = 'tile';
      t.textContent = idx + 1;
      t.onclick = function() {{ send({{toggle: idx, by: ROLE}}); }};
      grid.appendChild(t);
      tiles.push(t);
    }})(i);
  }}

  function render(state, lastBy, peers) {{
    for (var i = 0; i < N; i++) {{
      var on = !!state[i];
      tiles[i].classList.toggle('on', on);
    }}
    if (lastBy) document.getElementById('lastby').textContent = lastBy;
    if (peers != null) document.getElementById('peers').textContent = peers;
  }}

  function describeTransport(via) {{
    // The P2P viewer publishes the live path (direct vs TURN relay) here.
    var c = window.__llmingConn;
    if (c && c.transport === 'p2p') {{
      return c.path === 'turn' ? 'P2P · TURN relay' : (c.path === 'direct' ? 'P2P · direct' : 'P2P');
    }}
    if (c && c.transport === 'proxy') {{
      return c.path === 'e2e' ? 'proxy · E2E encrypted' : 'proxy relay';
    }}
    if (via === 'proxy') return 'proxy relay';
    if (via === 'p2p') return 'P2P';
    return via || 'direct';
  }}
  fetch(base + 'api/info', {{cache:'no-store'}})
    .then(function(r){{return r.json();}})
    .then(function(d){{ document.getElementById('transport').textContent = describeTransport(d.forwarded_via); }})
    .catch(function(){{}});

  var ws, retry = 0;
  function send(obj) {{ if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }}
  function connect() {{
    var u = new URL(base + 'ws/board', location.href);
    u.protocol = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    ws = new WebSocket(u.href);
    ws.onopen = function(){{ retry = 0; document.getElementById('status').textContent = 'live'; }};
    ws.onmessage = function(ev){{
      var m = JSON.parse(ev.data);
      render(m.state, m.last_by, m.peers);
      if (m.changed != null && tiles[m.changed]) {{
        tiles[m.changed].classList.remove('pulse');
        void tiles[m.changed].offsetWidth;
        tiles[m.changed].classList.add('pulse');
      }}
    }};
    ws.onclose = function(){{
      document.getElementById('status').textContent = 'reconnecting…';
      retry = Math.min(retry + 1, 10);
      setTimeout(connect, 300 * retry);
    }};
    ws.onerror = function(){{ try {{ ws.close(); }} catch(e) {{}} }};
  }}
  connect();
}})();
</script>
</body>
</html>
"""


class _Board:
    """In-memory shared state broadcast to every connected client."""

    def __init__(self, size: int) -> None:
        self.state = [False] * size
        self.last_by = ""
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def snapshot(self, changed: int | None = None) -> str:
        return json.dumps(
            {
                "type": "board",
                "state": self.state,
                "last_by": self.last_by,
                "peers": len(self._clients),
                "changed": changed,
            }
        )

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)
        await self.broadcast()

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        await self.broadcast()

    async def toggle(self, idx: int, by: str) -> None:
        if 0 <= idx < len(self.state):
            self.state[idx] = not self.state[idx]
            self.last_by = by or "?"
            await self.broadcast(changed=idx)

    async def broadcast(self, changed: int | None = None) -> None:
        payload = self.snapshot(changed)
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


def build_board_app(label: str = "board", *, pairing_url: str = "") -> FastAPI:
    app = FastAPI(title=f"knowledge-board-{label}")
    board = _Board(GRID)
    app.state.pairing_url = pairing_url

    def page(role: str, with_qr: bool) -> str:
        qr_block = ""
        if with_qr and app.state.pairing_url:
            qr_block = (
                '<div class="qr"><div id="qrbox"></div>'
                '<div class="hint">📱 scan with your phone to join the board</div></div>'
                '<script>fetch("qr.svg").then(function(r){return r.text();})'
                '.then(function(svg){document.getElementById("qrbox").innerHTML=svg;});</script>'
            )
        return PAGE.format(
            role=role,
            role_json=json.dumps(role),
            grid=GRID,
            qr_block=qr_block,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        role = request.query_params.get("role", "guest")
        with_qr = request.query_params.get("qr", "0") in ("1", "true", "yes")
        return HTMLResponse(page(role, with_qr))

    @app.get("/qr.svg")
    async def qr_svg() -> Response:
        url = app.state.pairing_url
        if not url:
            return Response("<svg/>", media_type="image/svg+xml")
        import io
        import re

        import segno

        buf = io.BytesIO()
        # border=4 is the standard QR quiet zone; without it scanners can miss it.
        segno.make(url, error="m").save(buf, kind="svg", scale=6, border=4, dark="#0d1117")
        svg = buf.getvalue().decode("utf-8")
        # segno omits viewBox; without it, CSS sizing clips instead of scaling.
        # (Locate the <svg> tag itself — the doc starts with an <?xml?> decl.)
        start = svg.find("<svg")
        tag = svg[start : svg.find(">", start) + 1]
        if "viewBox" not in tag:
            m = re.search(r'width="(\d+)"\s+height="(\d+)"', tag)
            if m:
                svg = svg.replace("<svg ", f'<svg viewBox="0 0 {m.group(1)} {m.group(2)}" ', 1)
        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/info")
    async def info(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "app": label,
                "forwarded_via": request.headers.get("x-forwarded-via", "direct"),
                "ok": True,
            }
        )

    @app.websocket("/ws/board")
    async def board_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await board.add(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg: dict[str, Any] = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if "toggle" in msg:
                    await board.toggle(int(msg["toggle"]), str(msg.get("by", "?")))
        except WebSocketDisconnect:
            pass
        finally:
            await board.remove(websocket)

    @app.post("/_llming/remote/verify-token")
    async def verify_token() -> JSONResponse:
        return JSONResponse({"valid": True})

    return app
