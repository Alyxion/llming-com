# MCP Courier — Deployment runbook (Azure)

> Companion to [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md) (the topology this
> provisions) and [`CONFIGURATION.md`](CONFIGURATION.md) (env vars).
>
> **Status: reference, not yet validated against a live subscription.** Treat
> the commands/IaC below as a worked design to review and adapt — not a
> push-button script. See [`INFRASTRUCTURE.md` §12](INFRASTRUCTURE.md#12-known-gaps--required-before-go-live)
> for code gaps to resolve first.
>
> **Never commit real values.** Every `<placeholder>` is supplied at deploy
> time; secrets go to Key Vault, not git. `deploy/courier/azure/local.settings.json` is
> git-ignored.

---

## 0. Prerequisites

- `az` CLI (logged in: `az login`) and the `func` Core Tools (v4).
- A subscription where you may create resources in **Germany West Central**.
- Decide the **network topology** (A: capability-gated public storage, or B:
  fully private) — see [`INFRASTRUCTURE.md` §4](INFRASTRUCTURE.md#4-network-topology--the-key-decision).
- Resolve known code gaps for Topology A (user-delegation SAS), or deploy
  Topology B first.

Set working variables (example placeholders — choose your own, keep them out of
git):

```bash
LOCATION=germanywestcentral
RG=<rg>                       # resource group
STORAGE=st<unique>            # payload storage account (3–24 lc alnum)
RUNTIME_STORAGE=stfn<unique>  # functions runtime storage
CONTAINER=exchange
FUNCAPP=func-<unique>
VAULT=kv-<unique>
LAW=law-<unique>              # log analytics workspace
```

---

## 1. Resource group + governance

```bash
az group create -n "$RG" -l "$LOCATION"

# Pin every resource in the RG to EU/DE (deny others).
az policy assignment create \
  --name courier-allowed-locations \
  --policy "e56962a6-4747-49cd-b67b-bf8b01975c4c" \
  --params '{ "listOfAllowedLocations": { "value": ["germanywestcentral","germanynorth","westeurope","northeurope"] } }' \
  --scope "$(az group show -n "$RG" --query id -o tsv)"
```

EU Data Boundary is a **tenant-level** setting — confirm it is enabled in the
admin centre (not scriptable per-RG).

---

## 2. Payload storage (private, HNS, locked down)

```bash
az storage account create \
  -g "$RG" -n "$STORAGE" -l "$LOCATION" \
  --sku Standard_ZRS --kind StorageV2 \
  --hns true \                      # hierarchical namespace → precise Set Expiry
  --min-tls-version TLS1_2 \
  --https-only true \
  --allow-blob-public-access false \
  --allow-shared-key-access false   # force identity/SAS-only

az storage container create \
  --account-name "$STORAGE" -n "$CONTAINER" \
  --auth-mode login --public-access off

# 30-day lifecycle backstop (precise expiry is set per-blob by the Function).
az storage account management-policy create \
  --account-name "$STORAGE" -g "$RG" \
  --policy '{ "rules": [ { "name": "expire-30d", "enabled": true, "type": "Lifecycle",
    "definition": { "filters": { "blobTypes": ["blockBlob"] },
      "actions": { "baseBlob": { "delete": { "daysAfterCreationGreaterThan": 30 } } } } } ] }'
```

**Topology B only** — disable public network and add a private endpoint:

```bash
az storage account update -g "$RG" -n "$STORAGE" --public-network-access Disabled
# then: az network private-endpoint create ... (target: blob sub-resource)
#       + private DNS zone privatelink.blob.core.windows.net linked to the VNet
```

Create the **runtime** storage account (`$RUNTIME_STORAGE`) the same way (it
backs `AzureWebJobsStorage`; keep it separate from payload data).

---

## 3. Key Vault + secrets

```bash
az keyvault create -g "$RG" -n "$VAULT" -l "$LOCATION" \
  --enable-rbac-authorization true --enable-purge-protection true

# Upload bearer keys (comma-separated) and the capability-token HMAC secret.
az keyvault secret set --vault-name "$VAULT" -n COURIER-API-KEYS  --value "<key1>,<key2>"
az keyvault secret set --vault-name "$VAULT" -n COURIER-SIGNING-KEY --value "$(openssl rand -base64 48)"
```

---

## 4. Observability

```bash
az monitor log-analytics workspace create -g "$RG" -n "$LAW" -l "$LOCATION"
# App Insights (workspace-based) is created with / linked to the Function App.
# Add diagnostic settings on the storage account + Function → this workspace.
```

---

## 5. Function App + identity

```bash
az functionapp create \
  -g "$RG" -n "$FUNCAPP" -l "$LOCATION" \
  --storage-account "$RUNTIME_STORAGE" \
  --flexconsumption-location "$LOCATION" \
  --runtime python --functions-version 4 \
  --assign-identity '[system]'

PRINCIPAL=$(az functionapp identity show -g "$RG" -n "$FUNCAPP" --query principalId -o tsv)
STORAGE_ID=$(az storage account show -g "$RG" -n "$STORAGE" --query id -o tsv)
VAULT_ID=$(az keyvault show -g "$RG" -n "$VAULT" --query id -o tsv)
# Role assignments (see INFRASTRUCTURE.md §5).
# Data OWNER (not Contributor) is required so the Function can call Set Blob
# Expiry (comp=expiry) over managed identity on an HNS account — Contributor
# yields AuthorizationPermissionMismatch. RBAC propagation can take a few minutes.
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Owner" --scope "$STORAGE_ID"
# Topology A only (identity-based SAS): also assign "Storage Blob Delegator".
# Hardened profile only: "Key Vault Secrets User" on "$VAULT_ID".
```

---

## 6. App settings (config + Key Vault references)

```bash
ACCOUNT_URL="https://$STORAGE.blob.core.windows.net"
# Host root only — routes are served under the 'courier' prefix (host.json) and
# download URLs add 'courier/o', so do NOT append /api or /courier here.
BASE_URL="https://$FUNCAPP.azurewebsites.net"

az functionapp config appsettings set -g "$RG" -n "$FUNCAPP" --settings \
  COURIER_ACCOUNT_URL="$ACCOUNT_URL" \
  COURIER_CONTAINER="$CONTAINER" \
  COURIER_PUBLIC_BASE_URL="$BASE_URL" \
  COURIER_DEFAULT_TTL_SECONDS=7200 \
  COURIER_MAX_TTL_SECONDS=2592000 \
  COURIER_MAX_UPLOAD_BYTES=104857600 \
  COURIER_DEFAULT_SINGLE_USE=false \
  COURIER_PREFER_DIRECT_SAS=false \
  COURIER_API_KEYS="@Microsoft.KeyVault(VaultName=$VAULT;SecretName=COURIER-API-KEYS)" \
  COURIER_SIGNING_KEY="@Microsoft.KeyVault(VaultName=$VAULT;SecretName=COURIER-SIGNING-KEY)"
# Minimal profile (no Key Vault): pass COURIER_API_KEYS / COURIER_SIGNING_KEY as
# literal values here instead of the @Microsoft.KeyVault(...) references.
# COURIER_PREFER_DIRECT_SAS=false forces Function-proxied downloads (Topology B).
```

---

## 7. Deploy the code

The `llming_com.courier` library is not on PyPI, so it is **vendored** into the
deployment package; the Flex Consumption remote build (Oryx) installs the
third-party deps from `requirements.txt`. `func` Core Tools are **not**
required — build a zip and deploy it with `az`:

```bash
deploy/courier/azure/build.sh                     # → dist/courier-funcapp.zip (vendors the package)
az functionapp deployment source config-zip \
  -g "$RG" -n "$FUNCAPP" --src dist/courier-funcapp.zip --build-remote true
```

After deploy (and after any RBAC change), allow a minute and verify the
functions registered:

```bash
az functionapp function list -g "$RG" -n "$FUNCAPP" --query "[].name" -o tsv
# expect: upload, download, delete   (served under /courier/* via host.json
#         routePrefix; if empty, check App Insights for an import error;
#         restart with: az functionapp restart -g "$RG" -n "$FUNCAPP")
```

---

## 8. Smoke test (against the deployed Function)

```bash
KEY=<one-of-COURIER-API-KEYS>
FN="https://$FUNCAPP.azurewebsites.net"

# Upload (plain, for a non-sensitive smoke test). Returns {url, expiry}.
curl -s -X POST "$FN/courier/upload?ttl=2h&encrypted=false" -H "Authorization: Bearer $KEY" --data-binary @file.bin

# Download (Topology B / single-use → Function; Topology A multi-read → blob SAS).
curl -s "<url-from-response>" -o out.bin
```

For a regulated payload, use the client library (encrypts client-side):

```python
from llming_com.courier import CourierClient
url = CourierClient(FN, api_key=KEY).upload(pdf_bytes, content_type="application/pdf")
```

---

## 9. Reference Bicep skeleton (validate before use)

A minimal IaC outline equivalent to §1–§6 (illustrative — fill modules,
review, `az bicep build` / `what-if` before deploying):

```bicep
@description('All resources pinned to an EU region.')
param location string = 'germanywestcentral'
param storageName string
param containerName string = 'exchange'
param funcAppName string
param vaultName string

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    // Topology B: publicNetworkAccess: 'Disabled'
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
  resource container 'containers' = {
    name: containerName
    properties: { publicAccess: 'None' }
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    policy: { rules: [ {
      name: 'expire-30d'
      enabled: true
      type: 'Lifecycle'
      definition: {
        filters: { blobTypes: [ 'blockBlob' ] }
        actions: { baseBlob: { delete: { daysAfterCreationGreaterThan: 30 } } }
      }
    } ] }
  }
}

// + Microsoft.KeyVault/vaults, Microsoft.Web/sites (functionapp, system identity),
//   Microsoft.OperationalInsights/workspaces, role assignments (Blob Data
//   Contributor on container, Blob Delegator on account, KV Secrets User),
//   and (Topology B) Microsoft.Network/{virtualNetworks,privateEndpoints,privateDnsZones}.
```

---

## 10. Rotation & teardown

- **Rotate** `COURIER_API_KEYS` / `COURIER_SIGNING_KEY` by updating the Key Vault
  secret (App Settings reference picks it up). Rotating the signing key
  invalidates outstanding single-use / Function-proxied URLs.
- **Teardown:** `az group delete -n "$RG"` removes everything (payload is
  transient anyway; default 2 h TTL).

---

## 11. Pre-production checklist

- [ ] Topology chosen (A or B) and storage network configured to match.
- [ ] User-delegation SAS wired (Topology A) — see INFRASTRUCTURE §12.1.
- [ ] `COURIER_PREFER_DIRECT_SAS` flag implemented (Topology B) — §12.2.
- [ ] Functions Python worker version confirmed/pinned — §12.3.
- [ ] Secrets in Key Vault, **not** App Settings literals; no keys in git.
- [ ] `allowedLocations` policy assigned; EU Data Boundary on.
- [ ] Diagnostic settings (Storage + Function) → EU Log Analytics; verified no
      payload/secret in logs.
- [ ] Lifecycle rule present; per-blob Set Expiry verified on a test upload.
- [ ] Rate limits/quotas on the Function; max upload size set.
- [ ] DPA on file (SECURITY §8.3); RoPA entry added.
```
