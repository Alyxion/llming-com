#!/usr/bin/env bash
# Build a deployable Azure Functions package for the Courier upload provider.
#
# Only the `llming_com.courier` subpackage is vendored — NOT the full llming-com
# framework. A stub top-level `llming_com/__init__.py` is written so that
# `import llming_com.courier...` does not trigger the framework's heavy
# `__init__` (fastapi/starlette/websockets/p2p), keeping the Function lean.
# Third-party deps come from requirements.txt (installed by the Flex Consumption
# remote build / Oryx).
#
# Usage:
#   deploy/courier/azure/build.sh [output-zip]   # default: ./dist/courier-funcapp.zip
# Then deploy with (see docs/courier/DEPLOYMENT.md):
#   az functionapp deployment source config-zip -g "$RG" -n "$FUNCAPP" \
#       --src dist/courier-funcapp.zip --build-remote true
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="${1:-$REPO/dist/courier-funcapp.zip}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp "$HERE/function_app.py" "$HERE/host.json" "$HERE/requirements.txt" "$STAGE/"

# Vendor the subpackage under a stub `llming_com` namespace package so the
# import path matches dev (`llming_com.courier...`) without importing the
# framework's top-level __init__.
mkdir -p "$STAGE/llming_com"
printf '"""Stub namespace package: only llming_com.courier ships in the Function.\n\nThe full llming-com framework __init__ (fastapi/starlette/websockets/p2p) is\ndeliberately NOT vendored here — the Courier Function needs only the courier\nsubpackage and its lean dependency set.\n"""\n' > "$STAGE/llming_com/__init__.py"
cp -R "$REPO/llming_com/courier" "$STAGE/llming_com/courier"

# Drop caches/compiled artefacts.
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
( cd "$STAGE" && zip -r -q "$OUT" . )
echo "built: $OUT ($(du -h "$OUT" | cut -f1))"
