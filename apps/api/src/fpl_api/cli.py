"""``fpl-api``: operational commands for the API service.

``fpl-api create-key`` prints a new random API key once, with the SHA-256 hash to place in
``FPL_API_KEYS_SHA256`` (operators) or ``FPL_WEB_API_KEYS_SHA256`` (the web proxy). Only the
hash is ever configured or stored (§35, §75).

``fpl-api create-invite --label <who> --days 14`` stores a hashed invitation code in the
database (``FPL_DATABASE_URL``) and prints the code once, for the invite-only beta.
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
    inv = sub.add_parser("create-invite", help="create a beta invitation code (printed once)")
    inv.add_argument("--label", default="beta", help="who the invitation is for (not secret)")
    inv.add_argument("--days", type=int, default=14, help="validity in days (0 = no expiry)")
    args = p.parse_args(argv)
    if args.cmd == "create-key":
        key = secrets.token_urlsafe(32)
        print(json.dumps({"api_key": key, "sha256": hash_key(key)}))
        print("Store the key in your secret manager now; it is not recoverable from the hash.")
    elif args.cmd == "create-invite":
        from fpl_api.accounts import UserStore  # noqa: PLC0415 (needs the database only here)
        from fpl_api.settings import Settings  # noqa: PLC0415
        from fpl_storage.db import make_engine  # noqa: PLC0415

        url = Settings().database_url
        if not url:
            print("FPL_DATABASE_URL is not set")
            return 2
        code = UserStore(make_engine(url)).create_invite(args.label, args.days or None)
        print(json.dumps({"invite_code": code, "label": args.label, "days": args.days}))
        print("Send the code to the invitee now; only its hash is stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
