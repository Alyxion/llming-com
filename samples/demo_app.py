"""Minimal interactive demo for llming-com.

Run:
    LLMING_AUTH_SECRET=demo PYTHONPATH=. python3 samples/demo_app.py

Then open http://localhost:8001 in a browser.
"""

import datetime
import json
import secrets
import time
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

from llming_com import (
    AuthManager,
    BaseController,
    BaseSessionEntry,
    BaseSessionRegistry,
    SessionDataStore,
    build_debug_router,
    command,
    CommandScope,
    get_auth,
    get_default_command_registry,
    run_websocket_session,
    AUTH_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)

# ── Session entry with custom fields ────────────────────────────


@dataclass
class DemoSessionEntry(BaseSessionEntry):
    nickname: str = ""
    connected_at: str = ""


class DemoRegistry(BaseSessionRegistry["DemoSessionEntry"]):
    pass


registry = DemoRegistry.get()

# ── Commands ────────────────────────────────────────────────────


@command("ping", description="Health check", scope=CommandScope.GLOBAL, http_method="GET")
async def cmd_ping():
    return {"pong": True, "time": datetime.datetime.now().isoformat()}


@command("set_nickname", description="Set your display nickname", scope=CommandScope.SESSION)
async def cmd_set_nickname(entry: DemoSessionEntry, controller: BaseController, nickname: str):
    entry.nickname = nickname
    await controller.send({"type": "nickname_set", "nickname": nickname})
    return {"status": "ok", "nickname": nickname}


@command("get_status", description="Get current session status", scope=CommandScope.SESSION)
async def cmd_get_status(session_id: str, entry: DemoSessionEntry):
    return {
        "session_id": session_id[:8] + "...",
        "user_id": entry.user_id,
        "nickname": entry.nickname,
        "connected_at": entry.connected_at,
        "ws_connected": entry.websocket is not None,
    }


@command("list_users", description="List all connected users", scope=CommandScope.GLOBAL)
async def cmd_list_users():
    sessions = registry.list_sessions()
    users = []
    for sid, entry in sessions.items():
        users.append({
            "session_id": sid[:8] + "...",
            "user_id": entry.user_id,
            "nickname": entry.nickname or "(none)",
            "connected_at": entry.connected_at,
            "online": entry.websocket is not None,
        })
    return {"users": users, "total": len(users)}


@command("broadcast", description="Send a message to all connected users", scope=CommandScope.SESSION)
async def cmd_broadcast(entry: DemoSessionEntry, controller: BaseController, text: str):
    sender = entry.nickname or entry.user_id
    count = 0
    for sid, other in registry.list_sessions().items():
        if other.websocket is not None:
            try:
                await other.websocket.send_json({
                    "type": "broadcast",
                    "from": sender,
                    "text": text,
                })
                count += 1
            except Exception:
                pass
    return {"sent_to": count}


@command("save_note", description="Save a note to the session data store", scope=CommandScope.SESSION)
async def cmd_save_note(session_id: str, controller: BaseController, text: str):
    ns = f"session:{session_id}:notes"
    key = f"note_{int(time.time() * 1000)}"
    SessionDataStore.put(ns, key, text)
    count = len(SessionDataStore.list_keys(ns))
    await controller.send({"type": "note_saved", "key": key, "total": count})
    return {"status": "saved", "key": key, "total_notes": count}


# ── WebSocket hooks ─────────────────────────────────────────────

cmd_registry = get_default_command_registry()


async def on_connect(entry: DemoSessionEntry, websocket: WebSocket):
    ctrl = BaseController(session_id="demo")
    ctrl.set_websocket(websocket)
    entry.controller = ctrl
    entry.connected_at = datetime.datetime.now().isoformat(timespec="seconds")
    await ctrl.send({"type": "welcome", "message": "Connected to llming-com demo"})


async def on_message(entry: DemoSessionEntry, msg: dict):
    ctrl: BaseController = entry.controller
    msg_type = msg.get("type", "")

    if msg_type == "heartbeat":
        await ctrl.send({"type": "heartbeat_ack"})
        return

    if msg_type == "command":
        cmd_name = msg.get("name", "")
        cmd_def = cmd_registry.get(cmd_name)
        if not cmd_def:
            await ctrl.send({"type": "error", "message": f"Unknown command: {cmd_name}"})
            return
        kwargs = msg.get("args", {})
        # Inject special parameters the framework normally provides
        import inspect
        sig = inspect.signature(cmd_def.handler)
        if "entry" in sig.parameters:
            kwargs["entry"] = entry
        if "controller" in sig.parameters:
            kwargs["controller"] = ctrl
        if "session_id" in sig.parameters:
            kwargs["session_id"] = ctrl.session_id
        try:
            result = await cmd_def.handler(**kwargs)
            await ctrl.send({"type": "command_result", "command": cmd_name, "result": result})
        except Exception as e:
            await ctrl.send({"type": "error", "message": str(e)})
        return

    await ctrl.send({"type": "echo", "original": msg})


async def on_disconnect(session_id: str, entry: DemoSessionEntry):
    ns = f"session:{session_id}:notes"
    SessionDataStore.clear_namespace(ns)


# ── FastAPI app ─────────────────────────────────────────────────

app = FastAPI(title="llming-com demo")
auth = get_auth()

# Debug router
import os
os.environ.setdefault("DEBUG_API_KEY", "demo-debug-key")
app.include_router(build_debug_router(registry, allowed_networks=["*"]))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Generate a session ID but DON'T register yet — registration happens on WS connect
    session_id, token = auth.make_auth_cookie_value()
    response = HTMLResponse(HTML_PAGE.replace("__SESSION_ID__", session_id))
    response.set_cookie(AUTH_COOKIE_NAME, token, httponly=True, samesite="lax")
    response.set_cookie(SESSION_COOKIE_NAME, session_id, httponly=True, samesite="lax")
    return response


@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    # Register session on first WS connect (not on page load — avoids ghost sessions)
    if not registry.get_session(session_id):
        entry = DemoSessionEntry(
            user_id=f"user_{session_id[:6]}", nickname="",
            connected_at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        registry.register(session_id, entry)
    await run_websocket_session(
        websocket,
        session_id,
        registry,
        on_connect=on_connect,
        on_message=on_message,
        on_disconnect=on_disconnect,
        log_prefix="DEMO",
    )


# ── HTML UI ─────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>llming-com demo</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: 'Segoe UI', system-ui, sans-serif; padding: 24px; display: flex; flex-direction: column; }
  h1 { color: #7fdbca; margin-bottom: 4px; font-size: 1.4em; }
  .sub { color: #888; font-size: 0.85em; margin-bottom: 16px; }
  .row { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  button {
    background: #16213e; color: #7fdbca; border: 1px solid #7fdbca44;
    padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.9em;
  }
  button:hover { background: #1a3a5c; }
  button:disabled { opacity: 0.4; cursor: default; }
  button.connect { background: #0f3460; border-color: #7fdbca; }
  button.connect.on { background: #1b4332; }
  input {
    background: #16213e; color: #e0e0e0; border: 1px solid #333;
    padding: 8px 12px; border-radius: 6px; font-size: 0.9em; width: 200px;
  }
  #log {
    background: #0f0f23; border: 1px solid #333; border-radius: 8px;
    padding: 12px; flex: 1; min-height: 0; overflow-y: auto; font-family: 'Fira Code', monospace;
    font-size: 0.82em; line-height: 1.6; white-space: pre-wrap;
  }
  .ts { color: #555; } .dir-in { color: #f78c6c; } .dir-out { color: #82aaff; }
</style>
</head>
<body>
<h1>llming-com interactive demo</h1>
<p class="sub">Session: __SESSION_ID__</p>

<div class="row">
  <button class="connect" id="btnConn" onclick="toggleWs()">Connect WebSocket</button>
</div>
<div class="row">
  <button onclick="sendCmd('ping')" id="b1" disabled>ping</button>
  <input id="nick" placeholder="nickname">
  <button onclick="sendCmd('set_nickname',{nickname:el('nick').value})" id="b2" disabled>set_nickname</button>
  <button onclick="sendCmd('get_status')" id="b3" disabled>get_status</button>
  <button onclick="sendCmd('list_users')" id="b5" disabled>list_users</button>
  <input id="note" placeholder="note text">
  <button onclick="sendCmd('save_note',{text:el('note').value})" id="b4" disabled>save_note</button>
  <input id="bcast" placeholder="broadcast message">
  <button onclick="sendCmd('broadcast',{text:el('bcast').value})" id="b6" disabled>broadcast</button>
</div>
<div id="log"></div>

<script>
const SID = "__SESSION_ID__";
let ws = null;
const el = id => document.getElementById(id);
const cmdBtns = ['b1','b2','b3','b4','b5','b6'];

function log(dir, data) {
  const t = new Date().toLocaleTimeString();
  const pre = dir === 'in' ? '<span class="dir-in">&larr; recv</span>' : '<span class="dir-out">&rarr; send</span>';
  const line = `<span class="ts">${t}</span> ${pre}  ${typeof data === 'string' ? data : JSON.stringify(data, null, 2)}`;
  const d = el('log');
  d.innerHTML += line + '\\n';
  d.scrollTop = d.scrollHeight;
}

function toggleWs() {
  if (ws && ws.readyState <= 1) { ws.close(); return; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/${SID}`);
  ws.onopen = () => {
    log('sys', '-- connected --');
    el('btnConn').textContent = 'Disconnect';
    el('btnConn').classList.add('on');
    cmdBtns.forEach(b => el(b).disabled = false);
  };
  ws.onmessage = e => { try { log('in', JSON.parse(e.data)); } catch { log('in', e.data); } };
  ws.onclose = () => {
    log('sys', '-- disconnected --');
    el('btnConn').textContent = 'Connect WebSocket';
    el('btnConn').classList.remove('on');
    cmdBtns.forEach(b => el(b).disabled = true);
    ws = null;
  };
}

function sendCmd(name, args) {
  if (!ws || ws.readyState !== 1) return;
  const msg = { type: 'command', name, args: args || {} };
  ws.send(JSON.stringify(msg));
  log('out', msg);
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
