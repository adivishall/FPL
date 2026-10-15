#!/usr/bin/env bash
# Build the extension for a deployment: dist/unpacked/ (Chrome → Extensions → "Load unpacked")
# and a reproducible ZIP of exactly those files (the Chrome Web Store upload), with its SHA-256.
#
#   ./build.sh --url https://<site> --name "<product name>" [--out <dir>]
#
# The source in src/ is itself loadable (it targets a local stack); the build only substitutes
# the web app's address and the product's name. Same inputs → byte-identical ZIP.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
url="" name="FPL Copilot" out="$here/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --url) url=$2; shift 2 ;;
    --name) name=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    *) echo "usage: build.sh --url <web app origin> [--name <product name>] [--out <dir>]" >&2; exit 2 ;;
  esac
done
[ -n "$url" ] || { echo "build.sh: --url is required (the web app's origin, e.g. https://example.com)" >&2; exit 2; }
python3 -I "$here/tools/build.py" --src "$here/src" --out "$out" --url "$url" --name "$name"
