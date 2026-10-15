#!/usr/bin/env bash
# Store screenshots (1280×800) of the built popup in its two states, rendered by the browser
# tests' Chromium: dist/store/popup-team.png and dist/store/popup-site.png.
#   ./tools/store_shots.sh --url https://<site> --name "<product>"
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
"$here/build.sh" "$@" --out "$here/dist" >/dev/null
mkdir -p "$here/dist/store"
STORE_SHOTS=1 EXT_DIR="$here/dist/unpacked" OUT_DIR="$here/dist/store" \
  npx playwright test -c "$here/playwright.config.ts" test/store.spec.ts
ls -la "$here/dist/store"
