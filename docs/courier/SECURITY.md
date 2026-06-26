# MCP Courier — Functional & Security Specification

> Working name: **MCP Courier** — a general-purpose inter-MCP exchange (the
> receipts app is merely its first consumer).
> Status: draft for review · drafted with Claude.

> **Note on identities.** This document is intentionally free of organisation
> names, account names, hostnames, and other infrastructure identifiers. The
> data-controlling party is referred to as **the Controller**; the cloud
> provider as **the Provider**. Supply concrete values only through
> environment configuration — never in committed source (see `AGENTS.md`).

**Purpose.** A flexible, payload-agnostic side channel that lets any MCP server
hand any payload (PDFs, images, datasets, archives, JSON — any bytes, large or
binary) to any other MCP server or host-side consumer, without the bytes ever
passing through the model's context window. Not tied to email, Microsoft Graph,
or any single workflow.

**Architecture (locked).** Cloud exchange on the Controller's Azure
subscription, region Germany West Central. Storage: Azure Blob Storage (private
container). Provider service: Azure Function (HTTP-triggered) for uploads. TTL:
default 2 hours, maximum 30 days; single-use downloads available per object
(uploader decides). Credentials: upload authenticated by an API key sent as
`Authorization: Bearer` on the POST. Download needs no credential — a long URL
carrying the blob SAS + decryption key. Cleanup delegated to native
Blob-Storage expiry/lifecycle (no hand-rolled timer). The entire upload is a
plain POST of the bytes with a bearer header — no SDK required; a client
library is optional sugar.

**Scope — general, not email-specific.** This is a generic inter-MCP exchange
for arbitrary payloads between any producer and consumer MCPs. The
mail-attachment → draft case is only an illustration; nothing in the design
depends on email, Graph, or the receipts workflow. The exchange never inspects
or interprets the bytes it carries.

## 1. Guiding principle & topology

MCP is a star: servers connect to a host, never to each other. A transfer
decomposes into a side channel (Azure Blob) carrying the payload and a
reference (the download URL) passed producer → consumer. The model moves the
URL; Azure moves the bytes. Only the tiny URL (not the payload) ever touches
context.

**Reachability.** The host-side MCP servers do the up/downloads over normal
internet to the Blob/Function endpoints — reachable without trouble, and the
sandbox is not in the data path. (This sidesteps the blocker where a sandbox
can't reach host loopback.)

```
uploader MCP ─encrypt(AES-256-GCM)─► PUT (Function, API key) ─► Blob (ciphertext, GWC, TTL)
                                       │ returns download URL:
                                       │   https://<acct>.blob.core.windows.net/<c>/<id>?<SAS, exp≤TTL>#k=<DEK>
model/host: passes that URL (a short string) to the consumer
downloader MCP ─GET blob via SAS─► ciphertext ─decrypt(#k)─► use ─► object auto-expires
```

## 2. Components

* **Provider = Azure Function (HTTP trigger), upload only.** Validates the API
  key, size and content-type; writes the ciphertext blob; stamps expiry
  metadata; returns the assembled download URL. Holds no plaintext and no
  decryption key (the key lives only in the URL fragment the uploader appends).
* **Storage = Azure Blob, private container, Germany West Central.** Public
  access disabled; access exclusively via short-lived SAS minted per object.
  Server-side encryption (Microsoft-managed keys) on by default as
  defense-in-depth.
* **Uploader MCP** — generates the data key, encrypts, calls the Function,
  assembles the capability URL.
* **Downloader MCP / host consumer** — parses the URL, downloads ciphertext via
  SAS, decrypts with the key from the fragment. No credential required.

## 3. Functional requirements

### 3.0 Wire protocol — no SDK required (core contract)

Two plain HTTP calls; a client library is optional convenience, never required.

* **Upload:** `POST <host>/courier/upload` with `Authorization: Bearer <api-key>`,
  optional `?ttl=` / `?singleUse=`, body = the bytes (raw, or pre-encrypted,
  §5.2). Response: JSON `{ url, expiry }`.
* **Download:** `GET <url>` → the bytes. No auth, no SDK. (`<url>` is the
  returned capability URL, served at `<host>/courier/o/{id}`.)

```bash
curl -s -X POST "$FN/courier/upload?ttl=2h" -H "Authorization: Bearer $KEY" --data-binary @file
curl -s "$URL" -o out.bin
```

The bytes are opaque to the exchange. Whether they're encrypted is the
producer's choice — mandatory for regulated payloads (§5.2), trivial via the
optional library, but not a precondition of the protocol.

### 3.1 Operations

* **Upload** — POST, auth `Authorization: Bearer`. Streaming/chunked;
  configurable max size (default 100 MB). Body is ciphertext for regulated
  payloads. Params: `ttl` (default 2 h, cap 30 d) and `singleUse`. Returns the
  download URL and expiry.
* **Download** — GET the URL (no auth). Multi-read objects: a direct Blob SAS
  (+ key fragment). Single-use objects: a Function-mediated one-time endpoint
  that streams the ciphertext then deletes the blob — still credential-free.
* **STAT** (optional) — HEAD via SAS for size/metadata without full download.
* **Early delete** — Function endpoint (API-key) or direct SAS-delete for
  right-to-erasure ahead of TTL.
* No container enumeration is ever exposed.

### 3.2 The reference (download URL) format

```
https://<acct>.blob.core.windows.net/<container>/<objectId>?<read-only SAS, expiry ≤ TTL>#k=<base64url DEK>[;n=<nonce>]
```

* `<objectId>`: 256-bit random, opaque, no PII.
* Key is in the URL **fragment** (`#…`) so it is never transmitted to Azure —
  Azure stores and serves only ciphertext and never sees the key.
* SAS is read-only, single-object, expiry pinned ≤ object TTL.

### 3.3 Transport

* HTTPS only — Function endpoint (upload) and Blob SAS (download). Presigned
  direct-to-Blob is the path for large objects (keeps bulk bytes off Function
  compute).
* Inline base64 escape hatch ≤ 256 KB only, discouraged/logged.

### 3.4 Metadata (blob metadata / index tags)

`objectId`, size, plaintext SHA-256 (verified after decrypt), content-type,
producer id, created + expiry timestamps, sensitivity label, crypto params
(`alg=AES-256-GCM`, nonce if not prepended). No filename PII in the blob name.

### 3.5 TTL, single-use & cleanup (default 2 h, max 30 days)

* Access cutoff = SAS expiry (default 2 h, ≤ object TTL); after it the URL is
  dead.
* Deletion delegated to native Blob-Storage capabilities — no hand-rolled
  timer:
  * Per-object expiry (Set Expiry / DeleteAfter) auto-deletes each blob at its
    TTL — precise to the 2 h default.
  * Lifecycle-management rule = the 30-day hard backstop (day-granular).
* Single-use (uploader's per-object flag): the download URL targets a one-time
  Function endpoint that streams the ciphertext then deletes the blob
  immediately; multi-read objects keep the direct-SAS URL.
* Upload rejects requested TTL > 30 days; unspecified ⇒ 2 h.

### 3.6 Integrity

Plaintext SHA-256 computed before encryption, verified after decryption;
AES-GCM auth tag guards the ciphertext. Mismatch → hard error. Nonce prepended
to ciphertext (12 bytes) or carried in metadata.

### 3.7 Observability

Function + Storage diagnostic logs to Log Analytics: upload/download/delete with
actor, objectId, size, result, timestamp — **never payload bytes, never the
key/SAS**. Metrics: object/byte counts, expiries, 403/expired-SAS hits.

## 4. Reference flow (any producer MCP → any consumer MCP)

```
producer MCP (any payload) → AES-256-GCM encrypt → POST Function (API key) → download URL H
model/host: passes the short URL H to any consumer MCP
consumer MCP → GET blob via SAS → decrypt(#k) → verify sha256 → use → TTL reaps it
```

The mechanism is identical regardless of payload type or the MCPs involved.
Representative use cases: mail attachment → e-mail draft; document/OCR →
analysis/summarisation; image-generation → storage/publishing; data-export →
import/ETL (large CSV/Parquet/archives); any MCP → any MCP needing to hand off
a file too large or too binary for the context window.

**Collapse-when-co-located (optional).** If a producer/consumer pair shares one
backend, that pair may be a single server-side tool with no exchange at all.
The exchange exists for the general case where they don't — the default
assumption.

## 5. Security guards

### 5.1 In transit
TLS 1.3 to the Function and to Blob; HTTPS-only enforced by container policy; no
plaintext endpoints.

### 5.2 At rest — client-side / end-to-end
* The exchange stores whatever bytes it receives, verbatim — encryption is the
  producer's responsibility, keeping the wire contract trivial (§3.0).
* **Required for regulated/sensitive payloads:** the producer encrypts with
  AES-256-GCM + a fresh random 256-bit key per object before POSTing, so Azure
  stores ciphertext only. The optional library does this in one call.
* The key travels solely in the URL fragment (`#k=`), generated by the producer
  and appended after upload — so neither the Function nor Blob ever receives it;
  this is what enables credential-free download.
* Non-sensitive payloads may be posted in clear, relying on Azure SSE +
  the capability URL. Classify conservatively (§5.6) — when unsure, encrypt.

### 5.3 The capability URL — explicit risk & handling
* The download URL is a **bearer capability**: SAS (locates + authorises) + key
  (decrypts) = full read of that one object for anyone holding it. Treat the URL
  as a secret.
* **Mitigations:** short default TTL (2 h); per-object + read-only SAS; optional
  single-use; never log the full URL, never surface it in user-facing output;
  redact in audit (store objectId only, not SAS/key).
* **Accepted trade-off:** the key (32 bytes), not the payload, transits the
  model/host context — fine for context-size goals; the residual exposure is the
  bearer-URL property above, accepted in exchange for credential-free download.

### 5.4 Authentication & authorisation
* **Upload:** API key as `Authorization: Bearer <key>` on the POST (never in the
  URL/query, so it stays out of logs); stored in the uploader MCP's secret
  config; rotated periodically. Only uploaders hold it.
* **Download:** none by design — security rests on the unguessable, expiring,
  read-only capability URL.
* **Storage account:** public access disabled, shared-key listing disabled where
  possible, access only via per-object SAS the Function mints; the Function's
  storage credential scoped to the single exchange container.

### 5.5 Infra hardening
Object names are random IDs (no producer-controlled paths → no
injection/traversal). Container blocks public access + enforces TLS +
encryption-on-PUT via policy. Function: least-privilege managed identity to the
container, secrets in App Settings/Key Vault, rate-limits/quotas to bound abuse
and cost.

### 5.6 Data residency, minimisation, retention (DSGVO) — summary
* Region Germany West Central; EU Data Boundary on; redundancy kept in-country;
  public network access disabled; Azure Policy `allowedLocations` restricts
  resources to DE/EU.
* Payload-agnostic ⇒ default to the most sensitive plausible classification;
  regulated payloads must be client-side-encrypted (§5.2), so Microsoft
  processes ciphertext only.
* Transient buffer (default 2 h, 30 d max), not an archive — the consuming
  system of record keeps the durable copy. Cryptographic erasure via key-discard
  + native expiry.

### 5.7 Audit & non-repudiation
Append-only diagnostic logs (Function + Storage) with actor, objectId, size,
outcome, timestamp — never payload, key, or SAS. Retain per compliance policy
(longer than payload TTL).

### 5.8 Threat model (defended against)
* **Context bloat** — only the URL enters context, never bytes.
* **Eavesdropping** — TLS in transit; AES-256-GCM client-side at rest.
* **Azure / storage compromise** — only ciphertext on Azure; key never sent
  there → no plaintext recoverable.
* **URL leakage** — bearer risk; bounded by 2 h TTL, read-only single-object
  SAS, optional single-use, no-logging, secret handling (§5.3).
* **Upload-key theft** — scoped to upload only (can't read others' objects);
  rotate; rate-limit.
* **Stale data** — SAS expiry (access) + native expiry (deletion) + 30-day
  lifecycle backstop.
* **Tampering** — GCM auth tag + plaintext SHA-256.
* **Residency/jurisdiction** — in-country region + DPA; client-side encryption
  limits exposure regardless.

### 5.9 Non-goals / assumptions
Not a CDN/archive; single trust domain. The capability-URL model intentionally
trades a bearer-token risk for credential-free downloads. Microsoft-managed SSE
assumed on; existing Azure governance (RBAC, diagnostic export) assumed in
place.

## 6. Minimal viable implementation (Azure)

* **Storage:** account in Germany West Central (HNS enabled for Set Expiry;
  redundancy LRS/ZRS in-country) + private container; HTTPS-only, public access
  disabled (private endpoint); EU residency enforced via EU Data Boundary +
  Azure Policy `allowedLocations` (DE/EU); lifecycle rule = delete after 30 days
  (backstop).
* **Upload Function** (HTTP), auth = `Authorization: Bearer`: validate
  token/size/type → store the posted bytes, set per-blob expiry (now+ttl,
  default 2 h, cap 30 d) and `singleUse` tag → mint read-only SAS (expiry = blob
  expiry) → return `{ url, expiry }`. For `singleUse`, return a one-time Function
  download URL instead of the raw SAS.
* **Deletion = native:** per-blob Set Expiry + lifecycle backstop (30 d);
  single-use blobs deleted by the one-time endpoint after streaming. No custom
  timer.
* **Producer side:** for sensitive payloads, AES-256-GCM-encrypt then POST and
  append `#k=`; otherwise POST as-is. Either way it's a single POST + bearer
  header — no SDK needed (§3.0).
* **Optional client library:** wraps encrypt → POST → append `#k` and parse →
  GET → verify → decrypt for convenience only.

This repository implements the above with a backend-agnostic core
(`ExchangeService`), an in-memory backend + FastAPI server for local testing,
and an Azure Blob backend + Azure Functions host for production.

## 7. Remaining decisions

* **Resolved:** single-use = per-object uploader flag · cleanup = native Blob
  Set Expiry + lifecycle · upload auth = `Authorization: Bearer` · client
  library = optional sugar.
* Default `singleUse` when unspecified (recommend `false` = multi-read in TTL)?
* HNS account for precise Set Expiry, or lifecycle-only (day-granular)?
* Sign-off on the §8 data-protection action checklist.

## 8. Data protection & DPA (DSGVO/GDPR) — detailed

> All references to the data-controlling party use the neutral term **the
> Controller**. Insert the concrete legal entity only in internal, non-committed
> records (the RoPA, the DPA file).

### 8.1 Roles
* **Controller:** the data-controlling organisation (determines purpose and
  means). The exchange is operated internally by the Controller.
* **Processor:** Microsoft (Azure) — processes ciphertext on the Controller's
  behalf for regulated payloads.
* Each producing/consuming MCP acts within the Controller's controllership. If a
  third-party-operated MCP is added, assess whether it becomes a separate
  processor needing its own Art. 28 contract.

### 8.2 Legal basis (Art. 6)
Expense/invoice handling rests on contract performance and/or legitimate
interest (orderly accounting). Record the chosen basis in the RoPA.

### 8.3 DPA instrument (Art. 28)
Covered by the Microsoft Products and Services Data Protection Addendum
incorporated into the Controller's Azure agreement — Art. 28(3)-compliant.
**Action:** confirm the current Microsoft DPA version is on file for the
subscription before production data flows; keep a copy with the RoPA.

### 8.4 EU residency — how it is ensured
* Region: Germany West Central (Germany/EU).
* Microsoft EU Data Boundary enabled → storage and routine processing stays
  within the EU.
* In-country redundancy: LRS or ZRS; if geo-redundancy is used, the paired
  region (Germany North) is also in Germany.
* Azure Policy `allowedLocations` restricts every resource to
  `germanywestcentral` / an EU set; deny others. Diagnostic logs pinned to an EU
  Log Analytics workspace.
* Private networking: public access disabled, private endpoint.
* No international transfer anticipated; the Microsoft DPA's EU SCCs apply as a
  fallback for any incidental non-EU support access.

### 8.5 Supplementary measure — client-side encryption (Schrems II posture)
Regulated payloads are encrypted before upload with keys never disclosed to
Microsoft, so Microsoft holds ciphertext only. A strong technical supplementary
measure: lawful-access or sub-processor exposure yields no plaintext. Record it
in the transfer-impact reasoning.

### 8.6 Technical & organisational measures (Art. 32)
* **Confidentiality:** client-side AES-256-GCM (+ Azure SSE), TLS 1.3, private
  container, bearer-auth uploads, capability-URL downloads, RBAC + managed
  identity, secrets in Key Vault.
* **Integrity:** GCM auth tag + plaintext SHA-256.
* **Availability/resilience:** in-region redundancy; transient data, low blast
  radius.
* **Pseudonymisation/minimisation:** random object IDs, no PII in blob names or
  metadata, ciphertext only, short TTL.
* **Evaluation:** audit logging to EU Log Analytics; periodic review.

### 8.7 Storage limitation & erasure (Art. 5, 17)
* Default retention 2 h, hard max 30 days; transient buffer, not the system of
  record.
* Deletion via native per-blob expiry + lifecycle backstop; cryptographic
  erasure (key lives only in the discardable URL) renders any remnant
  unrecoverable.
* Data-subject access/rectification/erasure handled at the system of record, not
  the buffer.

### 8.8 Records, DPIA, breach
* **Art. 30 RoPA:** add an entry (purpose, data categories, recipient =
  Microsoft processor, retention, TOMs, transfers).
* **DPIA:** likely low-risk given ciphertext-only + transient + EU-resident; run
  a short screening and record the conclusion.
* **Breach (Art. 33/34):** the 72 h controller-notification process applies;
  document that a storage-only compromise without the keys exposes no personal
  data.

### 8.9 Assurance
Reference Azure certifications relevant to a German controller: ISO/IEC
27001/27017/27018, SOC 1/2/3, BSI C5, in the vendor assessment.
