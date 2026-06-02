# Connectivity: P2P, Proxy, and Named Publishing

llming-com gives any app two ways to be reached from anywhere, behind one
unified, durable, bookmarkable URL:

| Transport | What it is | When it wins |
|-----------|-----------|--------------|
| **P2P** | Direct browser↔host WebRTC DataChannel | Lowest latency, no data through a server, free |
| **Proxy** | Browser → hub → outbound host tunnel | Always works, even through strict NAT/firewalls |

Both carry the **same traffic** — HTTP and WebSocket — so your app's session
WebSockets work identically over either one. You can publish an app as
`p2p`, `proxy`, or `p2p+proxy` (try direct first, fall back to the proxy).

## The two transports

### Proxy hub

The host keeps one outbound WebSocket to a hub; the browser reaches it at
`/proxy/{host}/…`. HTTP and WS are relayed as JSON envelopes.

```python
from llming_com import create_access_app
from llming_com.access.remote import InMemoryAccessStore, TunnelClient

store = InMemoryAccessStore()
store.create_user("me", "Str0ng-pass")
host = store.create_host("me", "My Mac")
hub = create_access_app(store)                                  # the hub (FastAPI)
tunnel = TunnelClient("https://hub.example.com", host.connection_key,
                      local_url="http://127.0.0.1:8000")        # runs on the host
await tunnel.run()
```

### P2P (WebRTC)

The host answers a browser SDP offer and exposes a DataChannel; the generic
viewer overrides `fetch`/`WebSocket` so the whole app loads over it.

```python
from fastapi import FastAPI
from llming_com import mount_p2p_host

app = FastAPI()
# ... your routes ...
mount_p2p_host(app, local_base="http://127.0.0.1:8000", mode="p2p")
# browser opens /p2p/viewer.html → direct WebRTC → your app
```

Signaling is HTTP (`POST /p2p/offer`) for LAN/self-hosted, or the relay worker
for remote NAT traversal (`llming_com/server/p2p/relay`).

## Named publishing — stable URLs + durable reconnect

The headline feature. Publish an app under an `account/app` slug and get a
**stable, secret-free, bookmarkable** URL:

```text
https://apps.example.com/acme/dashboard
```

```python
from llming_com import PublishRegistry, mount_publish

registry = PublishRegistry()
registry.publish("acme", "dashboard",
                 modes=["p2p+proxy"], host_id=host.host_id,
                 ttl_seconds=3600)                     # link lifetime (0 = forever)
mount_publish(hub_app, registry=registry, hub_base="https://hub.example.com")
invite = registry.issue_pairing("acme", "dashboard")  # one-time ?k=… (render as QR)
```

### How durability works

```mermaid
sequenceDiagram
  participant U as Phone (browser)
  participant H as Hub / Host
  Note over U,H: First time — scan QR / open ?k=… invite
  U->>H: POST /acme/dashboard/api/pair/redeem {token}
  H-->>U: device_credential (long-lived)
  U->>U: store credential in IndexedDB; strip secret from URL
  Note over U,H: Every load (incl. after powersave / next day)
  U->>H: POST /acme/dashboard/api/handshake {credential}
  H-->>U: fresh connection (p2p signal / proxy session)
  Note over U,H: connected — bookmarkable URL unchanged
```

- The **stable URL carries no secret** — it only names the app.
- Pairing is redeemed **once** into a **device credential** stored in the
  browser (IndexedDB, localStorage fallback).
- **Every load re-handshakes** for a *fresh* connection. The credential — not
  the connection — persists, so reload-after-powersave reconnects with **no
  re-scan**, until the link's `ttl_seconds` expires or you `revoke()` it.
- The session WebSocket reconnects automatically as part of this.

!!! tip "Reconnect window = link lifetime"
    A phone can sleep for 10 minutes or overnight and reconnect on reload. The
    short-lived reconnect token is only a fast-resume optimization; real
    durability comes from the stored device credential + a fresh handshake.

## One-call publishing (newcomers)

```python
import uvicorn
from fastapi import FastAPI
from llming_com import serve_published

app = FastAPI()
# ... your routes ...
registry, pairing_url = serve_published(app, account="acme", app_name="dashboard", port=8800)
print("share this once:", pairing_url)   # .../acme/dashboard?k=…
uvicorn.run(app, port=8800)
```

Needs the WebRTC extra: `pip install "llming-com[webrtc]"`.

## Backward compatibility

Named URLs are a friendly **front door** to the existing transports — they
resolve to the same `/proxy/{host}/…` browsing and the same P2P viewer, so old
links keep working unchanged:

- `GET /proxy/{host}/…` — direct proxy browsing (unchanged)
- `GET /t/{host}/{token}` — token login (unchanged)
- `GET /p2p/viewer.html` — raw P2P viewer (unchanged)
- `GET /{account}/{app}` — new durable launcher (resolves to the above)

## Backends

| Backend | Proxy | P2P signaling | Named publishing | Use |
|---------|-------|---------------|------------------|-----|
| **FastAPI** (`create_access_app`, `mount_p2p_host`, `mount_publish`) | ✅ | HTTP offer + relay | ✅ | Self-host / Azure / on-prem |
| **Cloudflare** (`server/p2p/relay`, hub worker) | ✅ | relay (Durable Object) | ✅ (KV) | Managed, scale-to-zero |

## Samples

```bash
poetry run python samples/proxy_host_demo.py   # proxy hub + tunnel
poetry run python samples/p2p_demo.py          # direct WebRTC
poetry run python samples/publish_demo.py      # named URL + pairing + durable reconnect
```

See also [Workflow](workflow.md) for the relay protocol diagrams.
