# llming P2P Relay: Cloudflare Backend

This directory contains the Cloudflare Worker backend for the reusable llming
P2P/proxy relay. It is intentionally product-neutral and lives under the server
P2P relay backend hierarchy:

```text
llming_com/server/p2p/relay/cloudflare/
```

OpenHort, private self-hosted deployments, and commercial services should all
use this protocol instead of forking their own incompatible relay.

## Five-Minute Setup

Use separate app and relay subdomains:

```text
apps.example.com        user-facing apps launcher, pairing page, cookies, IndexedDB
relay.example.com       normal relay for your deployment
test-apps.example.com   isolated apps launcher for full integration tests
test-relay.example.com  disabled-by-default integration-test relay
```

The apps origin and relay origin intentionally do different jobs. The apps
origin owns browser state and shows the recent/reachable apps list. The relay
origin only exposes the room/mailbox/WebSocket protocol. Test origins should use
different Worker names, different admission keys, and different routes. The test
relay must never share the production admission key.

Prerequisites:

- a Cloudflare account you control, with Workers enabled;
- `node` and `npx`;
- DNS records for the relay subdomains in that Cloudflare zone;
- a Cloudflare API token that can deploy Workers and attach routes for the zone.

Wrangler reads Cloudflare credentials from the environment. A typical local
`.env` looks like `.env.example`:

```bash
export CLOUDFLARE_ACCOUNT_ID='...'
export CLOUDFLARE_API_TOKEN='...'
export CLOUDFLARE_ZONE_ID='...'
export LLMING_APPS_HOST='apps.example.com'
export LLMING_RELAY_HOST='relay.example.com'
export LLMING_TEST_APPS_HOST='test-apps.example.com'
export LLMING_TEST_RELAY_HOST='test-relay.example.com'
export LLMING_RELAY_ROUTE='relay.example.com/*'
export LLMING_TEST_RELAY_ROUTE='test-relay.example.com/*'
```

The operator is expected to set up Cloudflare DNS as part of owning the
deployment. The included `scripts/setup-dns.sh` can create the two proxied DNS
records once. The recurring deploy/test scripts only deploy Workers and attach
Worker routes; they do not create DNS records.

Token permissions for the included scripts:

```text
Account: Workers Scripts: Edit
Account: Workers Routes: Edit
Zone: Zone: Read
```

Add this for the included first-time DNS setup helper:

```text
Zone: DNS: Edit
```

Create proxied DNS records once, either in the dashboard or with:

```bash
./scripts/setup-dns.sh
```

Default record names:

```text
apps.example.com        proxied DNS record for the user-facing apps host
relay.example.com       proxied DNS record for the Worker route
test-apps.example.com   proxied DNS record for the isolated test apps host
test-relay.example.com  proxied DNS record for the disabled-by-default test Worker route
```

By default the helper creates proxied `AAAA` records pointing at `100::`. This
is only there to make the hostname exist inside Cloudflare's proxy; the Worker
route decides which script handles traffic. Override
`LLMING_CLOUDFLARE_DNS_RECORD_TYPE` and
`LLMING_CLOUDFLARE_DNS_RECORD_CONTENT` if your zone uses a different pattern.

Generate one admission key per relay and keep only the hash in Worker config:

```bash
export LLMING_RELAY_ADMISSION_KEY="$(openssl rand -base64 32)"
printf '%s' "$LLMING_RELAY_ADMISSION_KEY" | shasum -a 256
```

Put the hash into `wrangler.toml` for the normal relay, then deploy it to an
isolated route:

```bash
npx wrangler deploy --config wrangler.toml --route 'relay.example.com/*'
```

For the test relay, keep `wrangler.test.toml` disabled in source control and use
the helper script below. It deploys an enabled config for a short window, runs
your test command, and deploys a disabled config again in an `EXIT` trap:

```bash
export LLMING_TEST_RELAY_ROUTE='test-relay.example.com/*'
export LLMING_TEST_RELAY_ADMISSION_HASH='sha256:<hex-sha256>'
export LLMING_TEST_RELAY_WINDOW_SECONDS=900

./scripts/run-test-relay.sh -- pytest tests/test_public_relay_e2e.py
```

Manual emergency disable:

```bash
LLMING_TEST_RELAY_ROUTE='test-relay.example.com/*' ./scripts/disable-test-relay.sh
```

The disabled state is:

```toml
[vars]
LLMING_TEST_RELAY = "1"
LLMING_TEST_RELAY_ENABLED_UNTIL = "1970-01-01T00:00:00Z"
```

When disabled, every relay operation except `/health` returns `403`.

## Deploy Details

1. Copy this directory into a deployment workspace.
2. Generate a host admission key and store only its hash:

```bash
export LLMING_RELAY_ADMISSION_KEY="$(openssl rand -base64 32)"
printf '%s' "$LLMING_RELAY_ADMISSION_KEY" | shasum -a 256
```

3. Put the hash into `wrangler.toml`:

```toml
[vars]
HOST_ADMISSION_KEY_HASHES = "sha256:<hex-sha256>"
```

4. Deploy to your isolated relay subdomain:

```bash
npx wrangler deploy --config wrangler.toml --route 'relay.example.com/*'
```

## Contract

Host operations require:

```http
Authorization: Bearer <host-admission-key>
```

The relay also accepts `X-Llming-Admission-Key` for constrained environments.
`X-OpenHort-Admission-Key` remains accepted for compatibility.

Dedicated relay paths:

```text
POST /{room}/register
POST /{room}/connect
GET  /{room}/pending
POST /{room}/respond
GET  /{room}/response?h={device_token_hash}
GET  /{room}/sdp-inbox
POST /{room}/sdp-send
GET  /{room}/config
WS   /{room}
```

No admission hash means protected operations fail closed. `/health` remains
public.

## Test Relay

`wrangler.test.toml` is disabled by default. Set the enable-until timestamp only
for the duration of an integration test run, then reset it to the past. Prefer
`scripts/run-test-relay.sh` so teardown happens automatically.

```toml
[vars]
LLMING_TEST_RELAY = "1"
LLMING_TEST_RELAY_ENABLED_UNTIL = "1970-01-01T00:00:00Z"
```

The Worker also recognizes the older `OPENHORT_TEST_RELAY` and
`OPENHORT_TEST_RELAY_ENABLED_UNTIL` variables so existing OpenHort deployments
can migrate without changing route code first.
