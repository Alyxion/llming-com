#!/usr/bin/env bash
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN}"
: "${CLOUDFLARE_ZONE_ID:?set CLOUDFLARE_ZONE_ID}"
: "${LLMING_APPS_HOST:?set LLMING_APPS_HOST, for example apps.example.com}"
: "${LLMING_RELAY_HOST:?set LLMING_RELAY_HOST, for example relay.example.com}"
: "${LLMING_TEST_APPS_HOST:?set LLMING_TEST_APPS_HOST, for example test-apps.example.com}"
: "${LLMING_TEST_RELAY_HOST:?set LLMING_TEST_RELAY_HOST, for example test-relay.example.com}"

RECORD_TYPE="${LLMING_CLOUDFLARE_DNS_RECORD_TYPE:-AAAA}"
RECORD_CONTENT="${LLMING_CLOUDFLARE_DNS_RECORD_CONTENT:-100::}"

node - "$CLOUDFLARE_API_TOKEN" "$CLOUDFLARE_ZONE_ID" "$RECORD_TYPE" "$RECORD_CONTENT" "$LLMING_APPS_HOST" "$LLMING_RELAY_HOST" "$LLMING_TEST_APPS_HOST" "$LLMING_TEST_RELAY_HOST" <<'NODE'
const [token, zoneId, type, content, ...hosts] = process.argv.slice(2);
const base = `https://api.cloudflare.com/client/v4/zones/${zoneId}/dns_records`;

async function cf(path, options = {}) {
  const res = await fetch(`${base}${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  const identicalExists = Array.isArray(body.errors) && body.errors.some((error) => error.code === 81058);
  if (identicalExists) {
    return { success: true, result: [], identicalExists: true };
  }
  if (!res.ok || !body.success) {
    throw new Error(JSON.stringify(body, null, 2));
  }
  return body;
}

async function upsert(host) {
  const query = `?type=${encodeURIComponent(type)}&name=${encodeURIComponent(host)}`;
  const existing = await cf(query);
  const payload = {
    type,
    name: host,
    content,
    proxied: true,
    ttl: 1,
    comment: "llming P2P relay Worker route hostname",
  };
  if (existing.result.length > 0) {
    const id = existing.result[0].id;
    await cf(`/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    console.log(`updated ${type} ${host} -> ${content} (proxied)`);
  } else {
    const created = await cf("", { method: "POST", body: JSON.stringify(payload) });
    if (created.identicalExists) {
      console.log(`exists ${type} ${host} -> ${content} (proxied)`);
    } else {
      console.log(`created ${type} ${host} -> ${content} (proxied)`);
    }
  }
}

Promise.all(hosts.map(upsert)).catch((error) => {
  console.error(error.message);
  process.exit(1);
});
NODE
