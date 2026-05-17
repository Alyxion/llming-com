# P2P Relay Backends

Relay backends implement the shared room registration, mailbox, SDP bridge, and
WebSocket signaling contract used by llming applications.

Current backends:

| Backend | Path | Purpose |
|---|---|---|
| Cloudflare Worker | `cloudflare/` | Simple self-hostable public relay using Durable Objects |

Every backend must keep the host/app contract deployment-neutral:

```text
endpoint + admission key + same transport client
```

Backends may differ in storage, rate limiting, and deployment tooling, but they
must not require product-specific host code.
