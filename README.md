# LLMing-Com

Reusable WebSocket session management, authentication, and debug API for real-time web applications.

LLMing-Com provides the communication infrastructure layer for building WebSocket-based applications on top of FastAPI/Starlette. It handles HMAC authentication, session lifecycle, WebSocket transport, runtime debugging, and in-memory data storage — so your application can focus on domain logic.

## Features

### HMAC Cookie Authentication

Secure, stateless cookie-based auth using HMAC-SHA256 — no external crypto libraries required.

- **Session tokens**: `sign_auth_token()` / `verify_auth_cookie()` — bind a session ID to a tamper-proof cookie.
- **Identity tokens**: `sign_identity_token()` / `verify_identity_cookie()` — user-level tokens with configurable TTL (default 7 days), useful for OAuth-style flows where the identity outlives individual sessions.
- **Cookie factory**: `make_auth_cookie_value()` generates a fresh session ID + signed token pair in one call.
- **Framework-agnostic**: Works with any ASGI framework exposing `request.cookies` (Starlette, FastAPI, etc.).
- **Zero external dependencies**: Uses only Python stdlib (`hashlib`, `hmac`, `secrets`).

### Session Registry

A generic, type-safe session store with automatic cleanup.

- **Generic base classes**: `BaseSessionEntry` and `BaseSessionRegistry[E]` — subclass to add domain-specific fields (user profile, model config, permissions, etc.).
- **Singleton pattern**: `MyRegistry.get()` returns a single shared instance per registry class.
- **TTL cleanup**: Background asyncio task evicts idle sessions without an active WebSocket (configurable interval and TTL).
- **Lifecycle hooks**: Override `on_session_expired()` to run cleanup logic (close resources, persist state, notify).
- **Thread-safe access**: Register, retrieve, remove, and list sessions safely from any context.

### WebSocket Transport

Complete WebSocket lifecycle management in a single function call.

- **`run_websocket_session()`**: Session lookup, accept, JSON message loop, disconnect handling — all wired up.
- **Hook-based**: Caller provides `on_connect`, `on_message`, `on_disconnect` async callbacks.
- **Connection superseding**: Optionally close an existing WebSocket when a new one connects for the same session (e.g. user opens a second tab). The old connection receives close code `4001`.
- **Message validation**: Automatic JSON parsing, non-dict rejection, and configurable max message size with silent skip for oversized payloads.
- **Graceful error handling**: Disconnect exceptions are caught cleanly; the session's WebSocket reference is always cleared on exit.

### Base Controller

A per-session controller with safe send, heartbeat, and rate limiting.

- **Safe send**: `await controller.send(msg)` serializes to JSON and sends. Returns `False` silently if the WebSocket is closed — no exception handling needed in your code.
- **Heartbeat**: Built-in `heartbeat` → `heartbeat_ack` handling out of the box.
- **Rate limiting**: Sliding-window rate limiter (default 30 requests/60 seconds). Call `check_rate_limit()` before processing expensive operations.
- **Extensible**: Subclass and override `handle_message()` and `cleanup()` for domain-specific logic.

### Thread-Safe Data Store

In-memory, namespace-scoped key-value store for transient session data.

- **`SessionDataStore`**: Class-level API — no instances to manage.
- **Namespaced**: Isolate data per session, per feature, or per concern (`put("uploads", key, bytes)`).
- **Operations**: `put`, `get`, `pop`, `list_keys`, `clear_namespace`, `clear_all`.
- **Any value type**: Store bytes, dicts, lists, or any Python object.
- **Thread-safe**: All operations protected by `threading.Lock()`.

### Debug API

Production-safe session inspection and diagnostics via REST endpoints.

- **`build_debug_router()`**: Creates a FastAPI `APIRouter` with three endpoints:
  - `GET {prefix}/sessions` — list all sessions with user info, idle time, and WebSocket status.
  - `GET {prefix}/sessions/{id}` — detailed session view with custom fields via `session_detail_hook`.
  - `POST {prefix}/sessions/{id}/ws_send` — forward a JSON message to a session's WebSocket handler (useful for triggering actions remotely during debugging).
- **Auth**: API key via `x-debug-key` header or `?key=` query param. Key is read from an environment variable you specify.
- **IP whitelist**: Restrict access to private networks by default (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Pass `["*"]` to allow all.
- **Extensible**: `extra_routes(router, registry)` callback lets you mount additional domain-specific debug endpoints on the same router with the same auth.

### Mock Auth (Testing)

Bypass OAuth for headless and E2E testing with synthetic users.

- **`register_mock_user()`** / **`build_mock_login_router()`**: Register mock profiles and get a `/mock-login?email=...` endpoint that sets auth cookies without touching any identity provider.
- **Gated**: Requires `LLMING_MOCK_USERS=1` environment variable — cannot activate in production by accident.

## Installation

```bash
pip install llming-com
```

Or from source with [Poetry](https://python-poetry.org/):

```bash
git clone https://github.com/Alyxion/llming-com.git
cd llming-com
poetry install
```

## Quick Start

### Session Management

```python
from dataclasses import dataclass
from llming_com import BaseSessionEntry, BaseSessionRegistry, make_auth_cookie_value

@dataclass
class ChatEntry(BaseSessionEntry):
    model: str = "gpt-4o"

class ChatRegistry(BaseSessionRegistry["ChatEntry"]):
    pass

# Create a session
session_id, token = make_auth_cookie_value()
registry = ChatRegistry.get()
entry = ChatEntry(user_id="user-42", model="claude-sonnet")
registry.register(session_id, entry)

# Look up later
entry = registry.get_session(session_id)
print(f"User {entry.user_id} using {entry.model}")
```

### WebSocket Endpoint

```python
from fastapi import FastAPI, WebSocket
from llming_com import run_websocket_session

app = FastAPI()

@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    registry = ChatRegistry.get()

    async def on_connect(entry, ws):
        await ws.send_text('{"type": "welcome"}')

    async def on_message(entry, msg):
        print(f"Received: {msg}")

    async def on_disconnect(sid, entry):
        print(f"Session {sid} disconnected")

    await run_websocket_session(
        websocket, session_id, registry,
        on_connect=on_connect,
        on_message=on_message,
        on_disconnect=on_disconnect,
    )
```

### Debug API

```python
from llming_com import build_debug_router

debug_router = build_debug_router(
    ChatRegistry.get(),
    api_key_env="MY_DEBUG_KEY",
    prefix="/debug",
)
app.include_router(debug_router)
# GET  /debug/sessions        — list all active sessions
# GET  /debug/sessions/{id}   — session detail + custom fields
# POST /debug/sessions/{id}/ws_send — forward a message via WebSocket
```

### Cookie Authentication

```python
from llming_com import sign_auth_token, verify_auth_cookie, get_auth_session_id

# Sign a session token and set it as a cookie
token = sign_auth_token("session-abc")
response.set_cookie("llming_auth", token, httponly=True, samesite="lax")

# Verify in a request handler
if verify_auth_cookie(request):
    session_id = get_auth_session_id(request)
    print(f"Authenticated session: {session_id}")
```

### Data Store

```python
from llming_com import SessionDataStore

# Store uploaded file data per session
SessionDataStore.put("session-abc", "avatar.png", image_bytes)

# Retrieve later
data = SessionDataStore.get("session-abc", "avatar.png")

# Clean up when session ends
SessionDataStore.clear_namespace("session-abc")
```

## Architecture

```
llming_com/
├── auth.py          # HMAC cookie signing/verification (stdlib only)
├── session.py       # BaseSessionEntry + BaseSessionRegistry (generic, singleton)
├── transport.py     # run_websocket_session() — WebSocket lifecycle
├── controller.py    # BaseController — send, heartbeat, rate limiting
├── data_store.py    # SessionDataStore — thread-safe in-memory store
├── debug.py         # build_debug_router() — FastAPI debug endpoints
└── mock_auth.py     # Mock user system for headless/E2E testing
```

## Running Tests

```bash
poetry install
poetry run pytest
```

## License

**LLMing-Com** is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.
