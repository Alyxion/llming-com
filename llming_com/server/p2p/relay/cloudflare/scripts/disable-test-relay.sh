#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SOURCE_CONFIG="$ROOT_DIR/wrangler.test.toml"

: "${LLMING_TEST_RELAY_ROUTE:?set LLMING_TEST_RELAY_ROUTE, for example test-relay.example.com/*}"
: "${LLMING_TEST_RELAY_ADMISSION_HASH:=sha256:disabled}"

TMP_CONFIG="$(mktemp -t llming-test-relay-disable.XXXXXX.toml)"
trap 'rm -f "$TMP_CONFIG"' EXIT

node - "$SOURCE_CONFIG" "$TMP_CONFIG" "$LLMING_TEST_RELAY_ADMISSION_HASH" <<'NODE'
const fs = require("fs");
const [source, output, hash] = process.argv.slice(2);
let text = fs.readFileSync(source, "utf8");
text = text.replace(
  /LLMING_TEST_RELAY_ENABLED_UNTIL = ".*"/,
  'LLMING_TEST_RELAY_ENABLED_UNTIL = "1970-01-01T00:00:00Z"'
);
text = text.replace(
  /HOST_ADMISSION_KEY_HASHES = ".*"/,
  `HOST_ADMISSION_KEY_HASHES = "${hash}"`
);
fs.writeFileSync(output, text);
NODE

npx wrangler deploy --config "$TMP_CONFIG" --route "$LLMING_TEST_RELAY_ROUTE"
