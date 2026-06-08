"""Shared *knowledge board* — built on llming-com SESSIONS (no raw WebSocket).

This module is **Python only**. All HTML/CSS/JS lives in sibling files
(``board.html``, ``board.js``, ``llming-pairing.js``) and is served from there;
the server inlines them at serve time (over the P2P/relay tunnel only
fetch/WebSocket are shimmed, not ``<script src>``, so the page is
self-contained).

Front and back talk through llming-com **session handlers**:

- **Server** declares ``@router.handler("board.toggle")`` and pushes updates with
  ``session.call("board.update", …)``.
- **Browser** (``board.js``) uses ``LlmingWebSocket`` — the library owns
  reconnect + heartbeat — and just sends ``{type:"board.toggle"}`` / receives
  ``board.update`` calls. The host's pairing UI is the reusable
  ``LlmingPairing`` drop-in component.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
import uuid
from dataclasses import dataclass

import llming_com
from fastapi import FastAPI, Request, WebSocket
from llming_com import BaseController, BaseSessionEntry, BaseSessionRegistry, run_websocket_session
from llming_com.ws_router import SessionRouter
from pydantic import BaseModel
from starlette.responses import HTMLResponse, JSONResponse, Response

GRID = 9
_HEARTBEAT_DEAD_AFTER = 40.0  # no heartbeat for this long ⇒ prune (client beats every 15s)
_HERE = pathlib.Path(__file__).parent
_STATIC = pathlib.Path(llming_com.__file__).parent / "static"


def _asset(name: str) -> str:
    return (_HERE / name).read_text(encoding="utf-8")


def _lib(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


@dataclass
class BoardSession(BaseSessionEntry):
    """A connected client. Board state is shared (app-level), not per-session."""


class BoardRegistry(BaseSessionRegistry[BoardSession]):
    pass


class BoardController(BaseController):
    pass


class ToggleEvent(BaseModel):
    toggle: int
    by: str = "?"


def _render_qr_svg(data: str) -> str:
    import io
    import re

    import segno

    buf = io.BytesIO()
    # border=4 is the standard QR quiet zone; without it scanners can miss it.
    segno.make(data, error="m").save(buf, kind="svg", scale=6, border=4, dark="#0d1117")
    svg = buf.getvalue().decode("utf-8")
    # segno omits viewBox; without it, CSS sizing clips instead of scaling.
    start = svg.find("<svg")
    tag = svg[start : svg.find(">", start) + 1]
    if "viewBox" not in tag:
        m = re.search(r'width="(\d+)"\s+height="(\d+)"', tag)
        if m:
            svg = svg.replace("<svg ", f'<svg viewBox="0 0 {m.group(1)} {m.group(2)}" ', 1)
    return svg


def build_board_app(label: str = "board", *, pairing_url: str = "", pairing_code: str = "") -> FastAPI:
    app = FastAPI(title=f"knowledge-board-{label}")
    registry = BoardRegistry()
    state = [False] * GRID
    meta = {"last_by": ""}
    router = SessionRouter()
    reaper_started = {"v": False}
    app.state.pairing_url = pairing_url
    app.state.pairing_code = pairing_code

    def live_sessions() -> list[BoardSession]:
        return [s for s in registry.list_sessions().values() if s.websocket is not None]

    async def push_state(changed: int | None = None) -> None:
        sessions = live_sessions()
        payload = {"state": state, "last_by": meta["last_by"], "peers": len(sessions), "changed": changed}
        for s in sessions:
            await s.call("board.update", payload)

    async def notify_pairing(pstate: str) -> None:
        for s in live_sessions():
            await s.call("board.pairing", pstate)

    app.state.notify_pairing = notify_pairing  # the launcher wires connect events here

    @router.handler("board.toggle")
    async def _toggle(session: BoardSession, event: ToggleEvent) -> None:
        if 0 <= event.toggle < GRID:
            state[event.toggle] = not state[event.toggle]
            meta["last_by"] = event.by or "?"
            await push_state(changed=event.toggle)

    async def _reaper() -> None:
        # Prune sessions that stopped heart-beating (sleep / network drop without a
        # clean close) so the connected count stays live. llming-com owns the
        # heartbeat; we just act on staleness.
        while True:
            await asyncio.sleep(20)
            now = time.monotonic()
            pruned = False
            for sid, s in list(registry.list_sessions().items()):
                if s.websocket is not None and now - s.last_heartbeat > _HEARTBEAT_DEAD_AFTER:
                    try:
                        await s.websocket.close()
                    except Exception:
                        pass
                    s.websocket = None
                    registry.remove(sid)
                    pruned = True
            if pruned:
                await push_state()

    def page(role: str, with_qr: bool) -> str:
        html = _asset("board.html")
        html = (
            html.replace("__ROLE__", role)
            .replace("__GRID__", str(GRID))
            .replace("__QR__", "1" if with_qr else "0")
            .replace("__CODE__", app.state.pairing_code if with_qr else "")  # host view only
        )
        # Inline (sub-resources aren't shimmed over the tunnel — only fetch/WS).
        html = html.replace("/*__LLMING_WS__*/", _lib("llming-ws.js"))
        pairing = f"<script>{_asset('llming-pairing.js')}</script>" if (with_qr and app.state.pairing_url) else ""
        html = html.replace("<!--__PAIRING__-->", pairing)
        html = html.replace("/*__BOARD__*/", _asset("board.js"))
        return html

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        role = request.query_params.get("role", "guest")
        with_qr = request.query_params.get("qr", "0") in ("1", "true", "yes")
        return HTMLResponse(page(role, with_qr))

    @app.get("/api/session")
    async def create_session(request: Request) -> JSONResponse:
        session_id = str(uuid.uuid4())
        registry.register(session_id, BoardSession(user_id=f"user-{session_id[:8]}"))
        scheme = "wss" if request.url.scheme == "https" else "ws"
        return JSONResponse({"sessionId": session_id, "wsUrl": f"{scheme}://{request.url.netloc}/ws/{session_id}"})

    @app.websocket("/ws/{session_id}")
    async def board_ws(websocket: WebSocket) -> None:
        session_id = websocket.path_params["session_id"]

        async def on_connect(entry: BoardSession, ws: WebSocket) -> None:
            controller = BoardController(session_id)
            controller.set_websocket(ws)
            controller.attach_session(entry)
            controller.mount_session_router(router)
            await controller.send({"type": "welcome", "session_id": session_id})
            if not reaper_started["v"]:
                reaper_started["v"] = True
                asyncio.create_task(_reaper())
            await push_state()

        async def on_message(entry: BoardSession, msg: dict) -> None:
            if entry.controller is not None:
                await entry.controller.handle_message(msg)

        async def on_disconnect(sid: str, entry: BoardSession) -> None:
            entry.websocket = None
            registry.remove(sid)
            await push_state()

        await run_websocket_session(
            websocket, session_id, registry,
            on_connect=on_connect, on_message=on_message, on_disconnect=on_disconnect,
        )

    @app.get("/qr.svg")
    async def qr_svg() -> Response:
        if not app.state.pairing_url:
            return Response("<svg/>", media_type="image/svg+xml")
        return Response(_render_qr_svg(app.state.pairing_url), media_type="image/svg+xml")

    @app.get("/code.svg")
    async def code_svg() -> Response:
        if not app.state.pairing_code:
            return Response("<svg/>", media_type="image/svg+xml")
        return Response(_render_qr_svg(app.state.pairing_code), media_type="image/svg+xml")

    # Serve the drop-in components as static files too, so apps that aren't behind
    # the tunnel can just <script src> them.
    @app.get("/llming-pairing.js")
    async def pairing_js() -> Response:
        return Response(_asset("llming-pairing.js"), media_type="application/javascript")

    @app.get("/board.js")
    async def board_js() -> Response:
        return Response(_asset("board.js"), media_type="application/javascript")

    @app.get("/api/info")
    async def info(request: Request) -> JSONResponse:
        return JSONResponse(
            {"app": label, "forwarded_via": request.headers.get("x-forwarded-via", "direct"), "ok": True}
        )

    @app.post("/_llming/remote/verify-token")
    async def verify_token() -> JSONResponse:
        return JSONResponse({"valid": True})

    return app
