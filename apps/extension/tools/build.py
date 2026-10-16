"""Assemble dist/unpacked and a reproducible ZIP from src (called by build.sh).

Substitutes the web app origin and product name in config.js and manifest.json; copies nothing
else than the files the extension needs; writes the ZIP with sorted entries and fixed
timestamps so the same inputs give the same bytes (checked by test/lib.test.mjs)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_NAME = "FPL Copilot"
FILES = ("manifest.json", "config.js", "lib.js", "popup.html", "popup.css", "popup.js")
ICONS = ("icon-16.png", "icon-32.png", "icon-48.png", "icon-128.png")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # the earliest ZIP timestamp: identical for every build


def check_url(url: str) -> str:
    u = urlsplit(url)
    local = u.hostname in {"localhost", "127.0.0.1", "[::1]", "::1"}
    origin_only = u.path in {"", "/"} and not u.query and not u.fragment
    if u.scheme not in {"https", "http"} or not u.netloc or not origin_only:
        raise SystemExit(
            f"build: --url must be an origin such as https://example.com (got {url!r})"
        )
    if u.scheme == "http" and not local:
        raise SystemExit("build: the web app must be served over HTTPS (http only for localhost)")
    return f"{u.scheme}://{u.netloc}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--name", default=DEFAULT_NAME)
    a = p.parse_args()
    src, out = Path(a.src), Path(a.out)
    url, name = check_url(a.url), a.name.strip()
    if not name or len(name) > 45:  # the Web Store's limit for the name
        raise SystemExit("build: --name must be 1–45 characters")
    if re.search(r'["\\<>&\x00-\x1f]', name):  # spliced into JSON and HTML as plain text
        raise SystemExit('build: --name may not contain " \\ < > & or control characters')

    unpacked = out / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    (unpacked / "icons").mkdir(parents=True)
    for f in FILES:
        text = (src / f).read_text(encoding="utf-8")
        if f == "config.js":
            text = re.sub(
                r'export const APP_URL = ".*?";', f'export const APP_URL = "{url}";', text
            )
            text = re.sub(
                r'export const PRODUCT_NAME = ".*?";',
                f"export const PRODUCT_NAME = {json.dumps(name)};",
                text,
            )
        elif f in {"manifest.json", "popup.html"}:
            text = text.replace(DEFAULT_NAME, name)
        (unpacked / f).write_text(text, encoding="utf-8")
    for i in ICONS:
        shutil.copyfile(src / "icons" / i, unpacked / "icons" / i)

    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    if len(manifest["description"]) > 132:
        raise SystemExit("build: the manifest description exceeds 132 characters with this name")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "extension"
    zip_path = out / f"{slug}-extension-{manifest['version']}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(q for q in unpacked.rglob("*") if q.is_file()):
            info = zipfile.ZipInfo(path.relative_to(unpacked).as_posix(), date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (out / f"{zip_path.name}.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    print(f"{zip_path}\nsha256 {digest}\nunpacked {unpacked}")


if __name__ == "__main__":
    main()
