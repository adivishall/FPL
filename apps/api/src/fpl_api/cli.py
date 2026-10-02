"""``fpl-api``: operational commands for the API service.

``fpl-api create-key`` prints a new random API key once, with the SHA-256 hash to place in
``FPL_API_KEYS_SHA256``. Only the hash is ever configured or stored (§35, §75).
"""

from __future__ import annotations

import argparse
import json
import secrets

from fpl_api.security import hash_key


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fpl-api")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-key", help="generate an API key and its SHA-256 hash")
    args = p.parse_args(argv)
    if args.cmd == "create-key":
        key = secrets.token_urlsafe(32)
        print(json.dumps({"api_key": key, "sha256": hash_key(key)}))
        print("Store the key in your secret manager now; it is not recoverable from the hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
