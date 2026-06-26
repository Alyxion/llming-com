# MCP Courier — Azure Infrastructure (end-to-end concept)

> Companion to [`SECURITY.md`](SECURITY.md) (functional & security spec) and
> [`DEPLOYMENT.md`](DEPLOYMENT.md) (provisioning runbook). This document
> describes the **target Azure topology end-to-end**: every resource, how they
> connect, which identity does what, where secrets live, and how each spec
> requirement maps onto a concrete control.
>
> **Status: a Minimal-profile (Topology B-lite) deployment is live and verified
> end-to-end** in Germany West Central. See [§12 Status & remaining gaps](#12-status--remaining-gaps)
> for what was resolved during deployment and what remains for hardening.
>
> **No infrastructure identities appear in this document.** Every concrete
> name/URL/key is supplied at deploy time via environment / Key Vault. Names
> below are generic placeholders (`<rg>`, `st<unique>`, `func-<unique>`, …).

---

## 1. Overview

MCP Courier is a payload-agnostic side channel: a producer MCP uploads (cipher)
bytes, gets back a short capability URL, and a consumer MCP downloads them —
the bytes never enter the model context. The cloud footprint is deliberately
small: **one HTTP Function (upload/admin) + one private Blob container
(payload) + supporting identity, secret, network and observability resources**,
all in **Germany West Central**.

```
                         ┌────────────────────────── Azure subscription ──────────────────────────┐
                         │  Resource group <rg>  ·  region: Germany West Central (EU)              │
                         │                                                                          │
 producer MCP            │   ┌───────────────┐        managed identity (Blob Data Contributor      │
 (host-side) ──POST────────▶ │ Function App  │◀────────  + Blob Delegator + KV Secrets User)       │
   bytes + Bearer key    │   │ (HTTP, Python)│           │                                          │
                         │   └──────┬────────┘           │  reads secrets                           │
                         │          │ put ciphertext      ▼                                          │
                         │          │ set expiry      ┌──────────────┐                              │
                         │          ▼                 │  Key Vault   │  COURIER_API_KEYS,           │
 consumer MCP            │   ┌───────────────┐        │              │  COURIER_SIGNING_KEY         │
 (host-side) ◀─GET───────────│ Blob Storage  │        └──────────────┘                              │
   ciphertext (SAS)      │   │ private cont. │                                                      │
   ──decrypt(#k)         │   │  "exchange"   │── lifecycle: delete after 30 d (backstop)            │
                         │   │  per-blob TTL │── per-blob Set Expiry: precise 2 h default           │
                         │   └───────────────┘                                                      │
                         │          │ diagnostics            ┌────────────────────┐                 │
                         │          └───────────────────────▶│ Log Analytics +    │ (EU workspace)  │
                         │   Function logs ─────────────────▶│ Application Insights│                 │
                         │                                    └────────────────────┘                 │
                         │   Governance: Azure Policy allowedLocations(DE/EU) · EU Data Boundary on  │
                         └──────────────────────────────────────────────────────────────────────────┘
```

The model/host only ever moves the short capability URL between the two MCPs.

---

## 2. Resource inventory

| # | Resource | Type / SKU | Purpose | Key settings |
|---|----------|-----------|---------|--------------|
| 1 | **Exchange storage account** | Storage account, StorageV2, **HNS enabled**, LRS or ZRS | Holds the ciphertext payload blobs | Public blob access **off**; shared-key access **off**; min TLS 1.2; HTTPS-only; infra-encryption optional |
| 2 | **Exchange container** | Blob container (private) `exchange` | The single payload container (no enumeration exposed) | Private; lifecycle policy attached |
| 3 | **Function App** | Function App, Linux, Python worker (Flex Consumption or Premium) | The upload/admin provider (`POST /upload`, one-time `GET /o/{id}`, `DELETE /o/{id}`) | System-assigned managed identity; VNet integration (Topology B); App Settings → Key Vault references |
| 4 | **Functions runtime storage** | Storage account (separate) | `AzureWebJobsStorage` for the Functions host (metadata/leases) | Not the payload account; locked down identically |
| 5 | **Key Vault** | Key Vault (RBAC mode) | Stores `COURIER_API_KEYS`, `COURIER_SIGNING_KEY`, optional account key | Purge protection on; private endpoint (Topology B) |
| 6 | **Log Analytics workspace** | Log Analytics (EU region) | Diagnostic sink for Function + Storage | Retention per compliance policy (> payload TTL) |
| 7 | **Application Insights** | App Insights (workspace-based) | Function telemetry/metrics | Request sampling; **no payload/secret logging** |
| 8 | **VNet + subnets** | Virtual network | Private connectivity (Topology B) | Subnet for Function integration; subnet for private endpoints |
| 9 | **Private endpoints + Private DNS** | Private endpoint × {blob, vault, (function)} | Keep traffic on the Azure backbone (Topology B) | `privatelink.blob.core.windows.net`, `privatelink.vaultcore.azure.net` |
| 10 | **Azure Policy assignment** | Policy: `allowedLocations` | Pin all resources to DE/EU | Deny non-EU regions |
| 11 | **User-assigned identity** *(optional)* | Managed identity | Alternative to system-assigned if shared across apps | Same role assignments as §5 |

Resources 8–9 apply only to the network-isolated topology (see §4 Topology B).

---

## 3. End-to-end data flows (mapped to resources)

### 3.1 Upload
1. Producer MCP (host-side) AES-256-GCM-encrypts the payload client-side with a
   fresh 256-bit key (§5.2). The key **never leaves the producer**.
2. `POST <function>/courier/upload` with `Authorization: Bearer <api-key>` and the
   ciphertext body → **Function App** (#3).
3. Function validates the bearer key (against Key Vault-sourced
   `COURIER_API_KEYS`), size and content-type; generates a 256-bit random
   `objectId`; writes the blob to the **exchange container** (#2) via managed
   identity; stamps metadata (§3.4) and **per-blob Set Expiry = now + TTL**.
4. Function assembles the capability URL **without the key** and returns
   `{ url, expiry }`. The producer appends `#k=<key>` locally.

### 3.2 Multi-read download (Topology A — direct SAS)
1. Function (at upload) mints a **read-only, single-object SAS** (expiry ≤ object
   TTL) and returns a direct blob URL.
2. Consumer MCP `GET`s `https://<acct>.blob.core.windows.net/exchange/<id>?<SAS>`
   straight from **Blob Storage** (#1) — bulk bytes bypass Function compute.
3. Consumer decrypts with `#k`, verifies plaintext SHA-256, uses it.
4. Blob auto-deletes at its TTL (Set Expiry; 30-day lifecycle backstop).

### 3.3 Single-use download (both topologies — Function-mediated)
1. The capability URL targets the **Function** one-time endpoint
   (`GET /courier/o/{id}?<token>`), not the blob directly.
2. Function validates the token, streams the ciphertext from Blob, then
   **deletes the blob immediately** (§3.5).

### 3.4 Early delete (right-to-erasure)
`DELETE <function>/courier/o/{id}` with the bearer key → Function deletes the blob
ahead of TTL via managed identity.

### 3.5 Expiry / cleanup (no custom timer)
- **Per-blob Set Expiry** deletes each blob precisely at `now + TTL` (default
  2 h). Requires an HNS account (#1).
- **Lifecycle management rule** deletes anything older than 30 days (day-granular
  backstop).

---

## 4. Network topology — the key decision

The spec contains a deliberate tension: §3.2 publishes a **public blob URL +
SAS** for direct consumer downloads, while §5.6 wants **public network access
disabled**. These cannot both hold for an *external* consumer MCP. Two coherent
topologies resolve it; pick one per deployment.

### Topology A — capability-gated public storage (spec-literal, recommended default)
- Storage account network: **public endpoint enabled**, but:
  - anonymous/public blob access **off** (container private),
  - shared-key access **off** → access only via per-object, short-lived,
    read-only **SAS** minted by the Function,
  - HTTPS-only, min TLS 1.2, optional storage firewall IP allowlist.
- **Direct-to-blob downloads work** for any consumer MCP over the internet,
  gated solely by the unguessable expiring capability URL (§5.3).
- Bulk bytes stay off Function compute. Best fit for the general inter-MCP case.

### Topology B — fully private storage (max network isolation)
- Storage `publicNetworkAccess = Disabled` + **private endpoint** in the VNet;
  Function uses **VNet integration** to reach it.
- Consumers **cannot** hit the blob directly → **all downloads are
  Function-mediated** (server-mediated tokens; the Function proxies the bytes).
- Stronger network posture, but the Function is in the data path for every
  download (more compute/egress). Requires resources #8–#9.
- **Code touchpoint:** set `AzureBlobBackend.supports_direct_sas()` effectively
  off (a `COURIER_PREFER_DIRECT_SAS=false` flag — see §12) so the service always
  routes downloads through `GET /courier/o/{id}`.

| Aspect | Topology A | Topology B |
|--------|-----------|-----------|
| Direct-to-blob download | ✅ yes | ❌ Function-proxied |
| Storage public network | enabled (SAS-gated) | disabled (private endpoint) |
| Bulk bytes off Function | ✅ | ❌ |
| Extra VNet/PE resources | no | yes (#8–#9) |
| Residency/network control | strong (SAS + region) | strongest |

Either way: client-side encryption (§5.2) means Azure only ever holds
ciphertext, so the residual exposure is the bearer-URL property (§5.3),
bounded by short TTL — independent of topology.

---

## 5. Identity & RBAC

No connection strings or account keys for data-plane access — the Function uses
its **system-assigned managed identity** (#3) with least-privilege role
assignments, all scoped as tightly as possible:

| Principal | Role | Scope | Why |
|-----------|------|-------|-----|
| Function managed identity | **Storage Blob Data Owner** | exchange storage **account** | read/write/delete payload blobs **and Set Blob Expiry** (see note) |
| Function managed identity | **Storage Blob Delegator** | exchange storage **account** | mint **user-delegation SAS** for direct downloads (Topology A only) |
| Function managed identity | **Key Vault Secrets User** | the Key Vault (#5) | read `COURIER_API_KEYS`, `COURIER_SIGNING_KEY` (Hardened profile) |
| Deploy principal (CI/operator) | Owner/Contributor + RBAC Admin | resource group | provisioning only; not used at runtime |

> **Why Data Owner, not Data Contributor (verified on the live deploy).** The
> per-blob **Set Blob Expiry** operation (`comp=expiry`) on a hierarchical-
> namespace account is **not** covered by *Storage Blob Data Contributor* —
> over AAD/managed identity it fails with `AuthorizationPermissionMismatch`.
> *Storage Blob Data Owner* is required for precise TTL stamping. (Shared-key
> auth bypasses RBAC, which masks this in key-based testing.) If you do **not**
> need precise per-blob expiry and rely solely on the 30-day lifecycle
> backstop, Data Contributor suffices.

Shared-key access is **disabled** on the payload account; the Blob Delegator
role is what would allow identity-based SAS without an account key (Topology A).

---

## 6. Secret & key management

| Secret | Lives in | Consumed by | Notes |
|--------|----------|-------------|-------|
| `COURIER_API_KEYS` (upload bearer keys) | **Key Vault** → App Setting via `@Microsoft.KeyVault(...)` reference | Function (`server/auth.py`) | rotate periodically; comma-separated |
| `COURIER_SIGNING_KEY` (capability-token HMAC) | **Key Vault** | Function (`tokens.py`) | ≥ 32 random bytes; rotation invalidates outstanding single-use/proxied URLs |
| Storage **account key** | *avoid* — prefer managed identity; if used, Key Vault only | Function | only needed if not using user-delegation SAS |
| **Per-object data-encryption key (DEK)** | **nowhere server-side** | producer & consumer only | generated client-side, rides in the URL `#k=` fragment; the cloud never sees it (§5.2) |

The DEK is the crux: the entire confidentiality argument rests on it living
**only** in the capability-URL fragment, never in Azure, never in a log.

---

## 7. Storage configuration detail

- **Account:** StorageV2, **hierarchical namespace ON** (required for precise
  per-blob Set Expiry), LRS or ZRS (in-country redundancy; geo-paired region is
  Germany North — also in DE), min TLS 1.2, HTTPS-only, blob public access off,
  shared-key off, soft-delete optional (weigh against erasure obligations).
- **Container `exchange`:** private; object names are 256-bit random ids (no
  producer-controlled paths → no traversal); no PII in names/metadata.
- **Per-blob Set Expiry:** Function sets `Absolute` expiry = `now + TTL` on PUT
  (default 2 h, cap 30 d).
- **Lifecycle policy:** `delete` blobs where `daysAfterCreationGreaterThan: 30`
  — the day-granular backstop catching anything the precise expiry missed.

---

## 8. Function App configuration

- **Runtime:** Python v2 programming model (`deploy/courier/azure/function_app.py`),
  `host.json` functionTimeout 5 min. **Verify the Azure Functions Python worker
  supports the pinned interpreter before deploy (see §12).**
- **Plan:** Flex Consumption (scales to zero, supports VNet) or Premium (if a
  warm VNet-integrated plan is required for Topology B).
- **App Settings:** `COURIER_ACCOUNT_URL`, `COURIER_CONTAINER`,
  `COURIER_PUBLIC_BASE_URL`, `COURIER_DEFAULT_TTL_SECONDS`,
  `COURIER_MAX_TTL_SECONDS`, `COURIER_MAX_UPLOAD_BYTES`,
  `COURIER_DEFAULT_SINGLE_USE`, plus Key Vault references for
  `COURIER_API_KEYS`/`COURIER_SIGNING_KEY`. (Full table in
  [`CONFIGURATION.md`](CONFIGURATION.md).)
- **Networking:** Topology B → VNet integration + private endpoints; Topology A
  → public ingress, protected by bearer auth + rate limits/quotas (§5.5).
- **Hardening:** least-privilege identity (§5), abuse/cost quotas, large uploads
  streamed (default cap 100 MB).

---

## 9. Observability

- Function + Storage **diagnostic settings → Log Analytics** (#6), Function
  telemetry → **Application Insights** (#7), both in an **EU** workspace.
- Logged: actor, `objectId`, size, operation, result, timestamp.
- **Never logged:** payload bytes, the DEK/`#k`, the SAS, or the full capability
  URL (§3.7, §5.3). The code logs `object_id` + size + result only.
- Metrics: object/byte counts, expiries, 403/expired-SAS hits.

---

## 10. Residency & governance

- **Region:** Germany West Central; all resources pinned via Azure Policy
  `allowedLocations` (DE/EU; deny others) (#10).
- **EU Data Boundary** enabled at tenant level → storage + routine processing
  stay in the EU.
- **In-country redundancy:** LRS/ZRS; if geo-redundant, paired region is also in
  Germany.
- Diagnostic/Log Analytics workspace pinned to EU.
- Maps to DSGVO posture in [`SECURITY.md`](SECURITY.md) §8.4–§8.6.

---

## 11. Spec → control traceability

| Spec requirement | Infra control |
|------------------|---------------|
| §3.5 precise 2 h expiry | per-blob Set Expiry (HNS) |
| §3.5 30 d backstop | lifecycle management rule |
| §3.2 capability URL | Function-minted read-only per-object SAS / HMAC token |
| §5.1 TLS 1.3 in transit | HTTPS-only + min TLS on storage & Function |
| §5.2 ciphertext at rest | client-side AES-256-GCM; SSE as defense-in-depth |
| §5.4 upload auth | Bearer key in Key Vault; managed identity for storage |
| §5.5 no path traversal | random 256-bit object ids |
| §5.6 residency | GWC + EU Data Boundary + `allowedLocations` |
| §3.7 audit | diagnostic settings → EU Log Analytics |
| §5.8 storage compromise | identity-only access, key never in Azure |

---

## 12. Status & remaining gaps

A **Minimal-profile (Topology B-lite) deployment has been provisioned and
verified end-to-end** in Germany West Central: upload (bearer), Function-proxied
download, client-side-encrypted round-trip, single-use one-shot, the 401/400
guards, and **precise per-blob Set Expiry** (confirmed via the `x-ms-expiry-time`
header) all work. Concrete resource names live only in the operator's
environment, never in git.

Resolved during deployment:

- ✅ **Python worker version** — Flex Consumption in GWC supports **Python 3.14**.
- ✅ **`COURIER_PREFER_DIRECT_SAS` flag** — implemented in `Settings` and
  honoured by `ExchangeService._download_url`; set `false` for Topology B.
- ✅ **Set Blob Expiry** — fixed to use the generated `set_expiry` operation
  (the public SDK has no `set_blob_expiry`); requires **Data Owner** (see §5).
- ✅ **Lifecycle backstop** — 30-day delete rule applied to the account.

Remaining gaps (apply to other topologies / hardening):

1. **User-delegation SAS not wired for managed identity (Topology A only).**
   `azure_host.build_service()` passes `user_delegation_key=None`; in
   `AzureBlobBackend.direct_sas_url`, `generate_blob_sas(... account_key=self._credential)`
   receives a *credential object*, not a signing key — so identity-based direct
   SAS will not sign. Fix: fetch `BlobServiceClient.get_user_delegation_key(...)`,
   cache/refresh (max 7 d), pass it through. The deployed Topology B avoids this
   entirely (Function-proxied downloads).
2. **Secrets in App Settings (Minimal profile).** The deployed profile keeps
   `COURIER_API_KEYS`/`COURIER_SIGNING_KEY` as App Settings, not Key Vault.
   Move to Key Vault references for the Hardened profile.
3. **Network isolation.** Minimal uses the public storage endpoint (private
   container, shared-key disabled). Add private endpoints + VNet for full
   Topology B.
4. **Single-use atomicity.** The one-time endpoint streams-then-deletes; a crash
   mid-stream leaves the blob until TTL (acceptable per §3.5, but noted).
5. **IaC not validated.** The Bicep/`az` in [`DEPLOYMENT.md`](DEPLOYMENT.md) is a
   reference; validate before relying on it.

---

## 13. Repository map

| Concern | Where |
|---------|-------|
| Function host (HTTP routes) | `deploy/courier/azure/function_app.py` |
| Host config (`host.json`) | `deploy/courier/azure/host.json` |
| Local settings template | `deploy/courier/azure/local.settings.json.example` |
| Build service from env | `llming_com/courier/azure_host.py` |
| Blob backend + SAS | `llming_com/courier/storage/azure_blob.py` |
| Core service logic | `llming_com/courier/service.py` |
| Config / env vars | `llming_com/courier/config.py` · [`CONFIGURATION.md`](CONFIGURATION.md) |
| Provisioning runbook | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| Functional & security spec | [`SECURITY.md`](SECURITY.md) |
