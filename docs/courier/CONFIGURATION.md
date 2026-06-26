# MCP Courier — Configuration reference

All runtime configuration is environment-driven (prefix `COURIER_`), read by
`llming_com/courier/config.py` (`Settings`) and, for storage-plane values,
`llming_com/courier/azure_host.py`. **No infrastructure value is ever
committed to git** — supply them via `.env` (local, git-ignored), Function App
settings, or Key Vault references. See [`.env.example`](../.env.example) and
[`deploy/courier/azure/local.settings.json.example`](../deploy/courier/azure/local.settings.json.example).

## Core settings (`Settings`)

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `COURIER_API_KEYS` | comma-separated | *(empty)* | Valid upload bearer keys. **Empty disables auth — dev only.** Source from Key Vault in production. |
| `COURIER_PUBLIC_BASE_URL` | URL | `http://localhost:8000` | Base URL embedded in returned download links — **host root only** (routes are served under `/courier`; download links add `/courier/o`, so do not append `/api` or `/courier`). In Azure: the Function host root (Topology B / single-use) or the blob account host (Topology A direct SAS, set on the backend). |
| `COURIER_CONTAINER` | string | `exchange` | Container/path segment in the capability URL. |
| `COURIER_SIGNING_KEY` | string | `dev-insecure-…` | HMAC secret for Function-mediated capability tokens. **Override everywhere real** (≥ 32 random bytes). |
| `COURIER_DEFAULT_TTL_SECONDS` | int | `7200` (2 h) | TTL when the uploader doesn't specify. |
| `COURIER_MAX_TTL_SECONDS` | int | `2592000` (30 d) | Hard maximum; uploads requesting more are rejected. |
| `COURIER_MAX_UPLOAD_BYTES` | int | `104857600` (100 MB) | Maximum upload body size. |
| `COURIER_DEFAULT_SINGLE_USE` | bool | `false` | `singleUse` when unspecified (recommended `false` = multi-read within TTL). |
| `COURIER_PREFER_DIRECT_SAS` | bool | `true` | `true`: multi-read downloads use a direct-to-blob SAS URL when the backend supports it (Topology A). `false`: force Function-mediated downloads (Topology B / managed-identity deploys without SAS signing). |

## Azure storage-plane settings (read in `azure_host.py`)

| Env var | Required | Meaning |
|---------|----------|---------|
| `COURIER_ACCOUNT_URL` | yes (prod) | `https://<acct>.blob.core.windows.net` — injected at deploy. |
| `COURIER_CONTAINER` | no | Overrides the core container value for the backend. |
| `COURIER_ACCOUNT_KEY` | no | Shared key. **Omit to use managed identity** (recommended). If set, store in Key Vault. |

## Per-request parameters (upload query string)

Sent on `POST /courier/upload?…` (not configuration, but the same vocabulary):

| Param | Default | Meaning |
|-------|---------|---------|
| `ttl` | `COURIER_DEFAULT_TTL_SECONDS` | `2h`, `30d`, `900s`, or bare seconds. |
| `singleUse` | `COURIER_DEFAULT_SINGLE_USE` | One-time download (stream-then-delete). |
| `contentType` | header / `application/octet-stream` | Stored content type. |
| `encrypted` | `true` | Whether the body is client-side ciphertext (metadata flag). |
| `producerId` | *(none)* | Opaque producer identity stamped in metadata. |
| `sensitivity` | `regulated` | Classification label (classify conservatively). |
| `sha256` | *(none)* | Plaintext digest, verified by the consumer after decrypt. |

## Notes

- The **per-object data-encryption key is never configured** — it is generated
  client-side per upload and lives only in the capability-URL `#k=` fragment.
- Booleans accept `1/true/yes/on` (case-insensitive).
- `Settings` also reads a local `.env` file automatically.
