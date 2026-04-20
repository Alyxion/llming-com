<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/llming-com/main/docs/logo-small.png" alt="LLMing Com" width="300"></p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14%2B-blue.svg" alt="Python 3.14+"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT"></a>
  <a href="https://pypi.org/project/llming-com/"><img src="https://img.shields.io/pypi/v/llming-com.svg" alt="PyPI"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/linter-ruff-blue.svg" alt="ruff"></a>
</p>

<p align="center"><strong>Real-time JS &harr; Python commands, AI-debuggable sessions, and MCP control &mdash; out of the box.</strong></p>

---

LLMing-Com connects JavaScript frontends to Python backends over WebSockets with structured commands, session management, cookie-based authentication, and a debug API that AI agents can use to inspect and control running applications.

## Why?

- **WS-first UI traffic** -- `WSRouter` gives you FastAPI-style namespaced dispatch for WebSocket JSON messages. One socket carries every UI command and query.
- **AI controls and debugs your app** -- The debug API and `@command` decorator expose a parallel HTTP/MCP surface for AI agents and tooling, separate from the UI socket.
- **One decorator, one debug command** -- Define a debug/admin command once with `@command`; get an HTTP endpoint, JSON schema, and MCP tool for free.
- **Sessions just work** -- Type-safe registry with TTL cleanup, WebSocket lifecycle management, and connection superseding built in.

## Transport Policy

Two surfaces, two router types -- pick by audience, not by preference:

| Audience | Transport | Router | Used for |
|---|---|---|---|
| UI / app frontend | WebSocket | `WSRouter` | All command and query traffic between the live frontend and backend |
| AI agents, MCP clients, ops tools | HTTP | `build_command_router` / `build_debug_router` | Debug/admin surface: session inspection, ws_send forwarding, `@command`-decorated debug actions |
| Anyone | HTTP | (your own FastAPI routes) | Large or static content only -- file uploads, blob downloads, asset serving |

Do not add HTTP routes for UI commands -- those belong on `WSRouter`. Do not push large blobs through the WS message pipe -- those belong on plain HTTP endpoints. The `@command` framework is for the debug/admin surface; it is not a UI command system.

## Features

- HMAC-SHA256 cookie authentication (session + identity tokens with expiry)
- Generic session registry with singleton pattern and TTL cleanup
- WebSocket transport with connection superseding and rate limiting
- **`WSRouter`** -- FastAPI-style namespaced dispatch for WS messages, nestable via `include()`, auto-replies with `_req_id` matching
- **JavaScript client** with auto-reconnect, heartbeat, and session-loss detection (framework-agnostic)
- Declarative `@command` framework for the debug/admin surface, with auto-generated REST + MCP endpoints
- Debug API with IP whitelisting, audit logging, and trusted proxy support
- Thread-safe in-memory data store with namespace isolation
- Mock auth system for headless and E2E testing
- MCP server (HTTP/SSE + stdio) for AI agent integration

## Quick Start

### UI commands (`WSRouter`)

Namespaced dispatch for WS JSON messages. Each module owns a `WSRouter(prefix=...)`; assemble them into a root router and dispatch from your controller. Handlers may return a dict -- the router auto-replies on the same socket and forwards `_req_id` for request/response matching.

```python
# windows.py
from llming_com import WSRouter

router = WSRouter(prefix="windows")

@router.handler("list")
async def list_windows(controller):
    return {"windows": [...]}

@router.handler("focus")
async def focus(controller, window_id: str):
    await controller.focus(window_id)
    return {"ok": True}

# app.py -- assemble and dispatch
from llming_com import WSRouter
root = WSRouter()
root.include(router)            # → windows.list, windows.focus
table = root.build_dispatch_table()

async def on_message(entry, msg):
    await root.dispatch(msg["type"], msg, entry.controller, _table=table)
```

### Debug commands (`@command`)

For the AI/MCP/debug surface only. Generates an HTTP REST endpoint *and* an MCP tool from a single declaration.

```python
from llming_com import command, CommandScope

@command("greet", description="Greet a user", scope=CommandScope.SESSION, http_method="POST")
async def greet(controller, name: str):
    await controller.send({"type": "greeting", "text": f"Hello, {name}!"})
    return {"status": "sent"}
```

### WebSocket

```python
from fastapi import FastAPI, WebSocket
from llming_com import run_websocket_session, BaseSessionRegistry

app = FastAPI()

@app.websocket("/ws/{session_id}")
async def ws(websocket: WebSocket, session_id: str):
    await run_websocket_session(
        websocket, session_id, BaseSessionRegistry.get(),
        on_message=lambda entry, msg: entry.controller.handle_message(msg),
    )
```

### Debug API

```python
from llming_com import build_debug_router

app.include_router(build_debug_router(registry, api_key_env="DEBUG_KEY"))
# GET  /debug/sessions
# GET  /debug/sessions/{id}
# POST /debug/sessions/{id}/ws_send
```

### JavaScript Client (auto-reconnect)

```html
<script src="/llming-com/llming-ws.js"></script>
<script>
const ws = new LlmingWebSocket('ws://localhost:8001/ws/my-session', {
  onMessage(msg)       { console.log('Got:', msg); },
  onReconnecting(info) { console.log(`Reconnecting ${info.attempt}/${info.maxAttempts}`); },
  onSessionLost(info)  { location.href = '/login'; },
});
ws.connect();
ws.send({ type: 'command', name: 'ping' });
</script>
```

Mount the static files from Python:

```python
from llming_com import mount_client_static
mount_client_static(app)  # serves /llming-com/llming-ws.js
```

Works with any JS framework (vanilla, Vue, React, Svelte, etc.). Features:
- Exponential-backoff reconnect (configurable max attempts and backoff)
- Heartbeat keepalive (15s default) with ack timeout — shows warning banner if server stops responding
- Handles llming-com close codes (4004 not-found, 4001 superseded)
- Optional built-in reconnect/warning banner (`showBanner: false` to disable)
- Offline mode: `new LlmingWebSocket(url, { offline: true })` makes `connect()` and `send()` no-ops; useful for self-contained demo bundles where every message is served by an in-page mock. Auto-detected when `window.__LLMING_OFFLINE__` is `true`.
- Zero dependencies, no DOM required (works in Web Workers too)

### Cookie Auth

```python
from llming_com import get_auth

auth = get_auth()
token = auth.sign_auth_token("session-abc")
response.set_cookie(auth.auth_cookie_name, token, httponly=True, secure=True, samesite="lax")

if auth.verify_auth_cookie(request):
    session_id = auth.get_auth_session_id(request)
    print(f"Authenticated: {session_id}")
```

**Always read cookie names from the `AuthManager` instance** (`auth.auth_cookie_name`, `auth.session_cookie_name`, `auth.identity_cookie_name`). Don't import the module-level `AUTH_COOKIE_NAME` etc. — those only resolve to the default-prefix names and defeat per-app isolation.

#### Unified auth across pages

`get_auth()` returns a process-wide **singleton**. Every page in an app — hub, chat, admin, anything else the host mounts — that calls `get_auth()` receives the same `AuthManager`, with the same HMAC secret and the same cookie names. Cookies set on `path="/"` during OAuth callback are readable by every subsequent request, so a user who authenticates on the hub page can navigate to the chat page without re-login, and vice versa. Identity tokens have a 30-day TTL by default; session tokens default to 24 hours.

The flow in a typical multi-page app (e.g. a landing hub + a chat page):

1. User visits `/` (hub). Host's `session_setup` callback calls `get_auth().verify_identity_cookie(request)`.
2. No valid identity → host returns `None`, hub calls `oauth_start_handler` → OAuth provider redirect.
3. Provider redirects back with `?code=…`. Host's `oauth_callback_handler` exchanges the code, calls `auth.sign_identity_token(sid)`, sets the identity + auth cookies.
4. User navigates to `/chat`. Chat's session factory calls the **same** `get_auth().verify_identity_cookie(request)`, gets a valid session, builds the chat session without a second OAuth round-trip.

The host chooses cookie scope: keep `path="/"` so every page under the domain sees the cookies.

#### Per-app isolation (`app_name`)

When two different llming-com apps share a domain (e.g. `example.com/chat` and `example.com/admin`, deployed independently), cookie-name collisions cause cross-talk. Solve this at the top of the host by constructing the singleton with an explicit `app_name`:

```python
# In your app's startup module, BEFORE any get_auth() call:
from llming_com.auth import AuthManager
import llming_com.auth as _auth_mod
_auth_mod._default_instance = AuthManager(app_name="myapp")
```

Once that's done, every subsequent `get_auth()` in any page returns the app-named manager. Cookies become `myapp_auth` / `myapp_session` / `myapp_identity`, cleanly separated from a sibling app using `otherapp_*`. Pages don't change — they already read names from the instance.

## Project Structure

```
llming_com/           Core library (auth, session, transport, commands, debug, data store)
llming_com/static/    JavaScript client (LlmingWebSocket)
tests/                Pytest suite
samples/              Example applications (run with: LLMING_AUTH_SECRET=demo python samples/demo_app.py)
docs/                 Documentation and assets
```

## Development

```bash
git clone https://github.com/Alyxion/llming-com.git
cd llming-com
poetry install
LLMING_AUTH_SECRET=dev-secret pytest tests/ -q
```

## License

MIT -- Copyright 2026 [Michael Ikemann](https://github.com/Alyxion)
