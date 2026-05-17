#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SOURCE_CONFIG="$ROOT_DIR/wrangler.test.toml"

if [ "$#" -eq 0 ]; then
  echo "usage: LLMING_TEST_RELAY_ROUTE='test-relay.example.com/*' LLMING_TEST_RELAY_ADMISSION_HASH='sha256:...' $0 -- <test-command>" >&2
  exit 2
fi

if [ "${1:-}" = "--" ]; then
  shift
fi

: "${LLMING_TEST_RELAY_ROUTE:?set LLMING_TEST_RELAY_ROUTE, for example test-relay.example.com/*}"
: "${LLMING_TEST_RELAY_ADMISSION_HASH:?set LLMING_TEST_RELAY_ADMISSION_HASH, for example sha256:<hex>}"

WINDOW_SECONDS="${LLMING_TEST_RELAY_WINDOW_SECONDS:-900}"
TMP_ENABLE="$(mktemp -t llming-test-relay-enable.XXXXXX.toml)"
TMP_DISABLE="$(mktemp -t llming-test-relay-disable.XXXXXX.toml)"

render_config() {
  local enabled_until="$1"
  local output="$2"
  node - "$SOURCE_CONFIG" "$output" "$LLMING_TEST_RELAY_ADMISSION_HASH" "$enabled_until" <<'NODE'
const fs = require("fs");
const [source, output, hash, until] = process.argv.slice(2);
let text = fs.readFileSync(source, "utf8");
text = text.replace(
  /LLMING_TEST_RELAY_ENABLED_UNTIL = ".*"/,
  `LLMING_TEST_RELAY_ENABLED_UNTIL = "${until}"`
);
text = text.replace(
  /HOST_ADMISSION_KEY_HASHES = ".*"/,
  `HOST_ADMISSION_KEY_HASHES = "${hash}"`
);
fs.writeFileSync(output, text);
NODE
}

deploy_config() {
  npx wrangler deploy --config "$1" --route "$LLMING_TEST_RELAY_ROUTE"
}

disable_relay() {
  render_config "1970-01-01T00:00:00Z" "$TMP_DISABLE"
  deploy_config "$TMP_DISABLE"
}

cleanup() {
  local status=$?
  set +e
  disable_relay
  rm -f "$TMP_ENABLE" "$TMP_DISABLE"
  exit "$status"
}

trap cleanup EXIT INT TERM

ENABLED_UNTIL="$(node -e "console.log(new Date(Date.now() + Number(process.argv[1]) * 1000).toISOString())" "$WINDOW_SECONDS")"
render_config "$ENABLED_UNTIL" "$TMP_ENABLE"
deploy_config "$TMP_ENABLE"

"$@"
