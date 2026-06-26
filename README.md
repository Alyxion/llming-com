<p align="center"><img src="https://raw.githubusercontent.com/Alyxion/llming-com/main/docs/logo-small.png" alt="LLMing Com" width="300"></p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14%2B-blue.svg" alt="Python 3.14+"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT"></a>
  <a href="https://pypi.org/project/llming-com/"><img src="https://img.shields.io/pypi/v/llming-com.svg" alt="PyPI"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/linter-ruff-blue.svg" alt="ruff"></a>
</p>

<p align="center"><strong>Where JavaScript, Python, and AI agents speak the same language.</strong></p>
<p align="center"><sub>Real-time JS &harr; Python commands, AI-debuggable sessions, and MCP control &mdash; out of the box.</sub></p>

---

LLMing-Com connects JavaScript frontends to Python backends over WebSockets with structured commands, session management, cookie-based authentication, and a debug API that AI agents can use to inspect and control running applications.

## Why?

- **WS-first UI traffic** -- `SessionRouter` and `AppRouter` give you FastAPI-style namespaced dispatch for WebSocket JSON messages. One socket carries every UI command and query.
- **AI controls and debugs your app** -- The debug API and `@command` decorator expose a parallel HTTP/MCP surface for AI agents and tooling, separate from the UI socket.
- **One decorator, one debug command** -- Define a debug/admin command once with `@command`; get an HTTP endpoint, JSON schema, and MCP tool for free.
- **Sessions just work** -- Type-safe registry with TTL cleanup, WebSocket lifecycle management, and connection superseding built in.

## Transport Policy

Two surfaces, two router types -- pick by audience, not by preference:

| Audience | Transport | Router | Used for |
|---|---|---|---|
| UI / app frontend | WebSocket | `SessionRouter` | Per-user command and query traffic between the live frontend and backend |
| UI / app frontend | WebSocket | `AppRouter` | App-wide commands with a typed app context |
| AI agents, MCP clients, ops tools | HTTP | `build_command_router` / `build_debug_router` | Debug/admin surface: session inspection, ws_send forwarding, `@command`-decorated debug actions |
| Anyone | HTTP | (your own FastAPI routes) | Large or static content only -- file uploads, blob downloads, asset serving |

Do not add HTTP routes for UI commands -- those belong on `SessionRouter` or `AppRouter`. Do not push large blobs through the WS message pipe -- those belong on plain HTTP endpoints. The `@command` framework is for the debug/admin surface; it is not a UI command system.

## Courier -- out-of-band byte transfer (`llming_com.courier`)

The "large or static content" HTTP lane has a first-class implementation: the
**Courier** subpackage, a payload-agnostic side channel for handing arbitrary
bytes between MCP servers (or any producer/consumer) **without the bytes ever
passing through the model's context window**. Object storage carries the
payload; a short capability URL is the only thing that moves -- *the model
moves the URL; the storage moves the bytes.*

```python
from llming_com.courier import CourierClient

client = CourierClient("https://courier.example", api_key="dev-key")  # host root
url = client.upload(pdf_bytes, content_type="application/pdf")  # POST /courier/upload, AES-256-GCM
data = client.download(url)                                     # GET /courier/o/{id} + decrypt + verify
```

All routes are served under `/courier` (`/courier/upload`, `/courier/o/{id}`,
`/courier/healthz`) so the Courier can be mounted alongside the rest of an
llming-com app. Embed it into an existing FastAPI app with
`app.include_router(build_router(service, settings))` from
`llming_com.courier.server.app`.

- **Lean core** -- importing the client/crypto/URL surface needs only `pydantic`
  + `cryptography` (already core here). The service/server/Azure paths are
  guarded behind extras so the framework stays unaffected.
- **Independently deployable** -- ships its own Azure Functions host. The build
  vendors *only* `llming_com.courier` (under a stub `llming_com` namespace) so
  the Function never pulls in the FastAPI/WebSocket/P2P framework.
- **Self-contained config** -- every deployment variable lives in
  [`deploy/courier/.env.example`](deploy/courier/.env.example); nothing
  infrastructure-identifying is committed.

Install the bits you need:

```bash
poetry install --extras courier-server   # local dev/upload FastAPI server
poetry install --extras courier-azure     # production Azure Blob backend + Function host
```

Run the local server and deploy to Azure:

```bash
# local dev server
export COURIER_API_KEYS="dev-key"
poetry run uvicorn llming_com.courier.server.app:create_app --factory --reload

# build + deploy the Azure Function (see docs/courier/DEPLOYMENT.md)
bash deploy/courier/azure/build.sh
```

Reference docs: [`docs/courier/`](docs/courier/) --
[SECURITY](docs/courier/SECURITY.md) (the spec),
[CONFIGURATION](docs/courier/CONFIGURATION.md) (every `COURIER_*` var),
[DEPLOYMENT](docs/courier/DEPLOYMENT.md),
[INFRASTRUCTURE](docs/courier/INFRASTRUCTURE.md).

## Shared P2P And Proxy Transport

`llming-com` is the canonical home for shared transport primitives used by
OpenHort and other llming applications.  Generic P2P, relay, proxy, pairing,
reconnect, DataChannel proxy, and browser viewer behavior should be implemented
here first and reused by product repositories.

This includes:

- named publishing: stable `apps.example.com/{owner}/{app-path}` URLs (owner is a
  user/org/api-key principal; the app path may be multi-segment, e.g.
  `com/samples/board`) with a link lifetime and durable reconnect
  (`PublishRegistry`, `mount_publish`, `serve_published`);
- the public relay HTTP/WebSocket contract;
- server-side P2P relay assets with Cloudflare as one deployment backend;
- opaque pairing-token redemption;
- paired-device credential helpers;
- reconnect grants for reload, phone sleep, and network interruption;
- DataChannel proxy message framing;
- generic browser viewer assets that can read stored credentials and initiate a
  fresh handshake.

Product repositories should configure and wrap these primitives, not fork them.
OpenHort uses this layer to access isolated agentic services; OpenHort
commercial/private code may add accounts, billing, tenant policy, and quotas
behind the same endpoint/key contract.

Pairing URLs should be bootstrap-only and opaque:

```text
https://example.com/p2p/pair#pt=<opaque-pairing-token>
```

After redemption, the viewer should store the paired-device credential in browser
storage and redirect to a stable bookmarkable URL such as:

```text
https://example.com/p2p/app
```

That stable page is responsible for reading IndexedDB/local storage/cookies,
requesting fresh handshakes, and reconnecting after reload or phone sleep.
Secrets and display metadata such as app name or icon should not be embedded in
bookmarkable URLs.

## Features

- HMAC-SHA256 cookie authentication (session + identity tokens with expiry)
- Generic session registry with singleton pattern and TTL cleanup
- One central `LlmingSession` type with typed, lazily-created session data attachments
- WebSocket transport with connection superseding and rate limiting
- **`SessionRouter` / `AppRouter`** -- typed namespaced dispatch for WS messages, nestable via `include()`, auto-replies with `_req_id` matching
- **JavaScript client** with auto-reconnect, heartbeat, and session-loss detection (framework-agnostic)
- Declarative `@command` framework for the debug/admin surface, with auto-generated REST + MCP endpoints
- Debug API with IP whitelisting, audit logging, and trusted proxy support
- Thread-safe in-memory data store with namespace isolation
- Mock auth system for headless and E2E testing
- MCP server (HTTP/SSE + stdio) for AI agent integration

## Usage

Runnable examples live in [`samples/`](samples/):

- `basic_session.py` — central sessions and typed data attachments
- `auth_demo.py` — HMAC tokens, identity tokens, tamper detection
- `websocket_server.py` — FastAPI WebSocket app with debug router
- `demo_app.py` — full interactive demo with the `@command` framework and the JavaScript client
- `p2p_demo.py` — **end-to-end P2P**: a browser loads an app over a direct WebRTC
  DataChannel (HTTP signaling, no relay). Pass `--proxy-fallback <hub-url>` to
  enable the `p2p+proxy` mode that falls back to the proxy hub when P2P fails.
- `proxy_host_demo.py` — **end-to-end proxy**: the access hub
  (`create_access_app`) + an outbound `TunnelClient` expose a local app at
  `/proxy/{host}/`. This is the P2P fallback transport.
- `publish_demo.py` — **named publishing**: a stable, bookmarkable URL
  (`/{owner}/{app-path}`) with a link lifetime, one-time QR/`?k=` pairing, and
  **durable reconnect** — reload after powersave / 10 minutes / the next day and
  it re-handshakes from a stored device credential, no re-scan. See
  [Connectivity](docs/manual/p2p/connectivity.md).

Run any sample with `LLMING_AUTH_SECRET=demo PYTHONPATH=. python samples/<name>.py`.
The P2P sample needs the optional WebRTC extra: `pip install "llming-com[webrtc]"`.

## Project Structure

```
llming_com/           Core library (auth, session, transport, commands, debug, data store)
llming_com/access/    Remote access tunnel primitives
llming_com/mcp/       MCP HTTP/SSE and stdio transports
llming_com/p2p/       P2P admission, DataChannel proxy, WebRTC peer, FastAPI signaling host
llming_com/courier/   Out-of-band byte transfer: crypto, capability URLs, storage backends, dev server
llming_com/static/    JavaScript client and generic P2P viewer assets
llming_com/server/p2p/ Server-side P2P relay assets and deployment backends
deploy/courier/       Azure Functions host + .env.example for the Courier
tests/                Pytest suite (tests/courier/ covers the Courier)
samples/              Example applications (run with: LLMING_AUTH_SECRET=demo python samples/demo_app.py)
docs/                 Documentation and assets (docs/courier/ for the Courier spec/runbooks)
```

## Documentation

The documentation site uses Material for MkDocs:

```bash
poetry run mkdocs serve -f mkdocs.yml
poetry run mkdocs build -f mkdocs.yml
```

The P2P workflow page demonstrates the Mermaid diagram lightbox used for
zoomable protocol and flow diagrams.

## Development

```bash
git clone https://github.com/Alyxion/llming-com.git
cd llming-com
poetry install
LLMING_AUTH_SECRET=dev-secret pytest tests/ -q
```

## License

MIT -- Copyright 2026 [Michael Ikemann](https://github.com/Alyxion)
