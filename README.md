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

- **AI controls the frontend** -- One `@command` decorator exposes a function as a REST endpoint *and* an MCP tool. An AI agent can list sessions, send messages, and trigger actions without custom glue code.
- **AI debugs your app** -- The debug API lets agents inspect sessions, view state, and forward WebSocket messages in real time.
- **One decorator = one command** -- Define a command once; get HTTP routing, parameter validation, JSON Schema, and MCP tooling for free.
- **Sessions just work** -- Type-safe registry with TTL cleanup, WebSocket lifecycle management, and connection superseding built in.

## Features

- HMAC-SHA256 cookie authentication (session + identity tokens with expiry)
- Generic session registry with singleton pattern and TTL cleanup
- WebSocket transport with connection superseding and rate limiting
- **JavaScript client** with auto-reconnect, heartbeat, and session-loss detection (framework-agnostic)
- Declarative `@command` framework with auto-generated REST + MCP endpoints
- Debug API with IP whitelisting, audit logging, and trusted proxy support
- Thread-safe in-memory data store with namespace isolation
- Mock auth system for headless and E2E testing
- MCP server (HTTP/SSE + stdio) for AI agent integration

## Quick Start

### Commands

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
- Zero dependencies, no DOM required (works in Web Workers too)

### Cookie Auth

```python
from llming_com import get_auth

auth = get_auth()
token = auth.sign_auth_token("session-abc")
response.set_cookie("llming_auth", token, httponly=True, secure=True, samesite="lax")

if auth.verify_auth_cookie(request):
    session_id = auth.get_auth_session_id(request)
    print(f"Authenticated: {session_id}")
```

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
