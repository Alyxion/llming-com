# Courier — curl quickstart for AI agents

Courier is an out-of-band byte channel. You **store** a payload once and get back a
short **capability URL**; any other agent **fetches** the bytes by dereferencing
that URL. The bytes never travel through a model's context window — only the URL
moves. Two plain HTTP calls, nothing else required.

```
producer agent  ──POST bytes──▶  Courier  ──returns URL──▶  producer agent
                                                                  │ hands URL over
consumer agent  ◀──bytes────────  Courier  ◀──GET URL─────────────┘
```

The store is a deployed **Azure Function** backed by Blob Storage. You do **not**
need the Python client, an SDK, or anything local — `curl` is a first-class
client. Everything below is copy-paste runnable against the deployed host.

## The two things you need

| Variable | What it is | Example |
|---|---|---|
| `BASE` | The deployed Courier (Azure Function) host. All routes live under `/courier`. | `https://<your-function-app>.azurewebsites.net` |
| `KEY` | Upload bearer key (one of `COURIER_API_KEYS`). **Only uploads/deletes need it.** Downloads need no credential. | `your-key` |

```bash
BASE="https://<your-function-app>.azurewebsites.net"
KEY="your-key"
```

> **Deployment prerequisite.** The Function App must have
> `COURIER_PUBLIC_BASE_URL` set to the same host as `BASE`. That value is baked
> into every returned download URL — if it is left at its default, uploads
> succeed but the URLs you get back point nowhere.

## Store data (upload)

`POST $BASE/courier/upload`, raw bytes in the body. Returns JSON with the
download URL. **Pass `encrypted=false`** for the pure-curl path — the bytes are
still encrypted at rest in Blob Storage; only the optional end-to-end
(client-held-key) layer is off. See [Encryption](#encryption).

```bash
# inline text
curl -sS -X POST "$BASE/courier/upload?encrypted=false&ttl=2h&contentType=text/plain" \
  -H "Authorization: Bearer $KEY" \
  --data-binary 'hello from agent A'

# a file
curl -sS -X POST "$BASE/courier/upload?encrypted=false&contentType=application/pdf" \
  -H "Authorization: Bearer $KEY" \
  --data-binary @report.pdf
```

Response (HTTP `201`):

```json
{
  "url": "https://<your-function-app>.azurewebsites.net/courier/o/9f3c…?se=…&sig=…",
  "expiry": "2026-06-26T15:00:00Z",
  "object_id": "9f3c…",
  "single_use": false
}
```

The `url` is the **only thing you pass on** to the consuming agent. Treat it as
a secret: whoever holds it can read the object until `expiry`.

### Upload query parameters

All are optional; sensible defaults apply.

| Param | Default | Meaning |
|---|---|---|
| `encrypted` | `true` | Metadata flag. Set **`false`** for curl-only exchange (still encrypted at rest). See [Encryption](#encryption). |
| `ttl` | `2h` | Lifetime. Accepts `900s`, `30m`, `2h`, `30d`, or a bare integer (seconds). Max 30 days. |
| `singleUse` | `false` | If `true`, the object is deleted the moment it is first downloaded (one-time handoff). |
| `contentType` | request `Content-Type` or `application/octet-stream` | MIME type returned on download. |
| `producerId` | — | Opaque label for who produced it (diagnostics only). |
| `sensitivity` | `regulated` | One of `public` / `internal` / `confidential` / `regulated` (metadata only). |
| `sha256` | — | Hex SHA-256 of the payload, if you want consumers to be able to verify integrity. |

## Fetch data (download)

Just `GET` the `url` from the upload response. No auth, no headers, no parsing —
the capability token is already baked into the query string.

```bash
# store and capture the URL in one step
URL=$(curl -sS -X POST "$BASE/courier/upload?encrypted=false&ttl=1h" \
        -H "Authorization: Bearer $KEY" \
        --data-binary @data.json | jq -r .url)

# any other agent fetches it — no key, no library
curl -sS "$URL"            # → bytes on stdout
curl -sS "$URL" -o out.json
```

That is the entire consumer side. Hand an agent the `url` and `curl "$url"` is
all it runs.

> A **multi-read** object's `url` is a direct-to-blob SAS link
> (`https://<account>.blob.core.windows.net/...`) so bulk bytes bypass Function
> compute; a **single-use** object's `url` routes back through the Function so it
> can delete after streaming. Either way the consumer does the same thing —
> `curl "$url"`.

## Single-use (one-time handoff)

`singleUse=true` makes the object self-destruct on first read — the clean
primitive for handing a secret to exactly one consumer.

```bash
URL=$(curl -sS -X POST "$BASE/courier/upload?encrypted=false&singleUse=true" \
        -H "Authorization: Bearer $KEY" --data-binary 'read me once' | jq -r .url)
curl -sS "$URL"   # → read me once
curl -sS "$URL"   # → 404 not_found (already consumed)
```

## Delete early (right-to-erasure)

Bearer-gated. Deletes ahead of TTL. Idempotent — deleting a gone object is fine.

```bash
curl -sS -X DELETE "$BASE/courier/o/$OBJECT_ID" -H "Authorization: Bearer $KEY"
```

## End-to-end (one agent stores, another fetches)

```bash
# Agent A stores and captures just the URL.
URL=$(curl -sS -X POST "$BASE/courier/upload?encrypted=false&ttl=1h" \
        -H "Authorization: Bearer $KEY" \
        --data-binary @data.json | jq -r .url)

# Hand $URL to agent B over any channel (it's short and context-safe).

# Agent B fetches the bytes — no key, no library.
curl -sS "$URL"
```

## Encryption

There are **two independent layers**. Do not confuse them:

1. **Encryption at rest — always on, nothing to do.** Blob Storage encrypts
   every object with always-on, platform-managed AES-256 (SSE) that cannot be
   disabled, so persisted bytes are never plaintext on disk. This applies even
   when you upload with `encrypted=false`.

2. **End-to-end encryption — optional, client-held key.** The producer encrypts
   the bytes (AES-256-GCM) *before* upload and appends the key to the URL
   fragment (`#k=…`), which HTTP never transmits, so the host stays key-blind.
   This is what `encrypted=true` (the default) signals. It gives confidentiality
   even against the Courier operator — but is **not** doable with curl alone; you
   need a crypto step on both sides (use `llming_com.courier.CourierClient`,
   which does `encrypt → POST → #k` and `GET → decrypt → verify` for you).

So **`encrypted=false` does not mean "plaintext on disk"** — it means "no
*end-to-end* layer; the operator can read the bytes." They are still encrypted at
rest, and download stays a single unauthenticated `GET`. This is the right choice
for curl-only, agent-to-agent exchange where you trust the Courier deployment and
TLS in transit. For data that must stay secret **even from the Courier operator**,
use end-to-end encryption (the `CourierClient`), not curl.

## Errors

Uniform JSON envelope `{ "code": "...", "message": "..." }`:

| HTTP | `code` | Cause |
|---|---|---|
| 400 | `validation_error` / `ttl_exceeded` | Bad param, or TTL over the 30-day max. |
| 401 | `unauthorized` | Missing/invalid bearer key on upload or delete. |
| 403 | `forbidden` | Download token missing, tampered, or expired. |
| 404 | `not_found` | Object missing, expired, or already consumed (single-use). |
| 413 | `payload_too_large` | Body over the configured max upload size (default 100 MB). |
| 422 | `integrity_error` | Decrypt or SHA-256 check failed (client-side, end-to-end path). |

## Notes for agents

- **The URL is the capability.** Anyone with it can read the object until it
  expires. Pass it only to the intended consumer; never log it.
- **Downloads need no credentials** — the token is in the URL. Only `upload` and
  `delete` use `Authorization: Bearer`.
- **The host exposes exactly three routes:** `POST /courier/upload`,
  `GET /courier/o/{id}`, and `DELETE /courier/o/{id}`. There is no stat/`HEAD` or
  health route on the deployed Function.
- Deployment/config reference lives in [`docs/courier/`](../../docs/courier/):
  [SECURITY](../../docs/courier/SECURITY.md) (the spec),
  [CONFIGURATION](../../docs/courier/CONFIGURATION.md) (every `COURIER_*` var),
  [DEPLOYMENT](../../docs/courier/DEPLOYMENT.md).
