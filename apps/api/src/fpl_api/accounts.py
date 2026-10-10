"""Invite-only accounts, sessions and manager ownership (restricted beta, §75 isolation).

Principals
----------
* **operator** — a key in ``FPL_API_KEYS_SHA256`` (operations, smoke tests, CI): full access.
* **user** — a valid session token (``Authorization: Bearer …``) issued at login: may only
  touch manager keys it owns; every manager-scoped request is checked server-side.
* **web** — a key in ``FPL_WEB_API_KEYS_SHA256`` and no session (the browser via the web proxy
  before sign-in): public reference data only.
* **anonymous** — no credential.

When the API does not require keys at all (development, most tests) every request is an
operator, as before; a session, when present, is still enforced.

Passwords are scrypt hashes with a per-user salt (standard library, no plaintext ever stored);
session tokens are 256-bit random values stored only as SHA-256 hashes; invite codes likewise.
Nothing in this module logs an e-mail address, password, token or code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import Engine, delete, select, update

from fpl_storage import models as m
from fpl_storage.db import session_scope

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**15, 8, 1, 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
MIN_PASSWORD = 10
SESSION_CACHE_SECONDS = 30.0


class AuthError(HTTPException):
    def __init__(self, status: int, detail: str, code: str) -> None:
        super().__init__(status, detail)
        self.code = code


def _exec(session: Any, statement: Any) -> Any:
    """Execute a DML statement; typed loosely so ``.rowcount`` is available to callers."""
    return session.execute(statement)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(key).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, key_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(key_b64)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


@dataclass(frozen=True)
class Principal:
    kind: str  # operator | user | web | anonymous
    user_id: str | None = None
    email: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.kind == "operator"


OPERATOR = Principal("operator")
WEB = Principal("web")
ANONYMOUS = Principal("anonymous")


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@dataclass
class UserStore:
    engine: Engine
    session_ttl: timedelta = timedelta(days=30)
    _cache: dict[str, tuple[float, Principal]] | None = None
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        self._cache, self._lock = {}, threading.Lock()

    # ------------------------------------------------------------------ invites
    def create_invite(self, label: str, days: int | None = 14) -> str:
        """Create an invitation; returns the code once (only its hash is stored)."""
        code = "inv_" + secrets.token_urlsafe(18)
        with session_scope(self.engine) as s:
            s.add(
                m.InviteRow(
                    code_hash=_digest(code),
                    label=label[:64],
                    expires_at=datetime.now(UTC) + timedelta(days=days) if days else None,
                )
            )
        return code

    # ------------------------------------------------------------------ accounts
    def register(self, invite_code: str, email: str, password: str) -> str:
        email = _normalise_email(email)
        if len(password) < MIN_PASSWORD:
            raise AuthError(422, f"password must have at least {MIN_PASSWORD} characters", "weak")
        now = datetime.now(UTC)
        with session_scope(self.engine) as s:
            inv = s.get(m.InviteRow, _digest(invite_code))
            if inv is None:
                raise AuthError(403, "invitation code not recognised", "invite_invalid")
            if inv.redeemed_at is not None:
                raise AuthError(410, "invitation code already used", "invite_used")
            if inv.expires_at is not None and inv.expires_at < now:
                raise AuthError(410, "invitation code expired", "invite_expired")
            if s.scalar(select(m.UserRow.id).where(m.UserRow.email == email)):
                raise AuthError(409, "an account with this e-mail already exists", "exists")
            uid = "usr_" + uuid.uuid4().hex[:20]
            s.add(m.UserRow(id=uid, email=email, password_hash=hash_password(password)))
            s.flush()
            inv.redeemed_by, inv.redeemed_at = uid, now
        return uid

    def login(self, email: str, password: str) -> tuple[str, Principal]:
        """Return a fresh session token and the principal, or raise 401 (timing-equalised: a
        missing account still runs the hash)."""
        email = _normalise_email(email)
        with session_scope(self.engine) as s:
            user = s.scalar(select(m.UserRow).where(m.UserRow.email == email))
            stored = user.password_hash if user else hash_password("not-the-password")
            ok = verify_password(password, stored) and user is not None
            if not ok or user is None or user.disabled_at is not None:
                raise AuthError(401, "e-mail or password not recognised", "login_failed")
            token = secrets.token_urlsafe(32)
            now = datetime.now(UTC)
            s.add(
                m.SessionRow(
                    token_hash=_digest(token),
                    user_id=user.id,
                    expires_at=now + self.session_ttl,
                    last_seen_at=now,
                )
            )
            user.last_login_at = now
            return token, Principal("user", user.id, user.email)

    def logout(self, token: str) -> None:
        h = _digest(token)
        with session_scope(self.engine) as s:
            _exec(
                s,
                update(m.SessionRow)
                .where(m.SessionRow.token_hash == h, m.SessionRow.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC)),
            )
        self._forget(h)

    def principal(self, token: str) -> Principal | None:
        """The user behind a session token, or None when it is unknown, expired or revoked."""
        h = _digest(token)
        now = time.monotonic()
        assert self._cache is not None and self._lock is not None
        with self._lock:
            hit = self._cache.get(h)
            if hit and hit[0] > now:
                return hit[1]
        with session_scope(self.engine) as s:
            row = s.get(m.SessionRow, h)
            if row is None or row.revoked_at is not None or row.expires_at < datetime.now(UTC):
                return None
            user = s.get(m.UserRow, row.user_id)
            if user is None or user.disabled_at is not None:
                return None
            row.last_seen_at = datetime.now(UTC)
            p = Principal("user", user.id, user.email)
        with self._lock:
            self._cache[h] = (now + SESSION_CACHE_SECONDS, p)
        return p

    def _forget(self, token_hash: str) -> None:
        assert self._cache is not None and self._lock is not None
        with self._lock:
            self._cache.pop(token_hash, None)

    def revoke_all(self, user_id: str) -> int:
        with session_scope(self.engine) as s:
            n = _exec(
                s,
                update(m.SessionRow)
                .where(m.SessionRow.user_id == user_id, m.SessionRow.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC)),
            ).rowcount
        assert self._cache is not None and self._lock is not None
        with self._lock:
            self._cache.clear()
        return int(n)

    def user(self, user_id: str) -> dict[str, Any] | None:
        with session_scope(self.engine) as s:
            u = s.get(m.UserRow, user_id)
            if u is None:
                return None
            return {
                "id": u.id,
                "email": u.email,
                "created_at": u.created_at.isoformat(),
                "analytics_opt_out": bool(u.analytics_opt_out),
                "managers": self._managers(s, user_id),
            }

    def set_preferences(self, user_id: str, analytics_opt_out: bool) -> None:
        with session_scope(self.engine) as s:
            _exec(
                s,
                update(m.UserRow)
                .where(m.UserRow.id == user_id)
                .values(analytics_opt_out=analytics_opt_out),
            )

    def analytics_opt_out(self, user_id: str) -> bool:
        with session_scope(self.engine) as s:
            return bool(
                s.scalar(select(m.UserRow.analytics_opt_out).where(m.UserRow.id == user_id))
            )

    # ------------------------------------------------------------------ managers
    @staticmethod
    def _managers(s: Any, user_id: str) -> list[dict[str, Any]]:
        rows = s.scalars(
            select(m.UserManagerRow)
            .where(m.UserManagerRow.user_id == user_id)
            .order_by(m.UserManagerRow.created_at)
        ).all()
        return [
            {"manager_key": r.manager_key, "label": r.label, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    def managers(self, user_id: str) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            return self._managers(s, user_id)

    def create_manager(self, user_id: str, label: str | None = None) -> str:
        key = "m_" + secrets.token_hex(8)
        with session_scope(self.engine) as s:
            s.add(m.UserManagerRow(manager_key=key, user_id=user_id, label=(label or None)))
        return key

    def owner_of(self, manager_key: str) -> str | None:
        with session_scope(self.engine) as s:
            row = s.get(m.UserManagerRow, manager_key)
            return row.user_id if row else None

    def owns(self, user_id: str, manager_key: str) -> bool:
        return self.owner_of(manager_key) == user_id

    def release_manager(self, manager_key: str) -> None:
        with session_scope(self.engine) as s:
            _exec(s, delete(m.UserManagerRow).where(m.UserManagerRow.manager_key == manager_key))

    def delete_user(self, user_id: str) -> dict[str, int]:
        """Remove the account itself (sessions, ownership rows, events, invite link). Manager
        data is erased by the caller first (``privacy.delete_manager`` per manager key)."""
        counts: dict[str, int] = {}
        with session_scope(self.engine) as s:
            counts["sessions"] = _exec(
                s, delete(m.SessionRow).where(m.SessionRow.user_id == user_id)
            ).rowcount
            counts["managers"] = _exec(
                s, delete(m.UserManagerRow).where(m.UserManagerRow.user_id == user_id)
            ).rowcount
            counts["events"] = _exec(
                s, delete(m.ProductEventRow).where(m.ProductEventRow.user_id == user_id)
            ).rowcount
            _exec(
                s,
                update(m.InviteRow)
                .where(m.InviteRow.redeemed_by == user_id)
                .values(redeemed_by=None),
            )
            counts["users"] = _exec(s, delete(m.UserRow).where(m.UserRow.id == user_id)).rowcount
        assert self._cache is not None and self._lock is not None
        with self._lock:
            self._cache.clear()
        return {k: int(v) for k, v in counts.items()}


# ---------------------------------------------------------------------- request authorisation


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        return tok or None
    return None


async def manager_key_in_request(request: Request) -> str | None:
    """The manager key a request names: path (``/managers/{key}``), query (``?manager_key=``)
    or JSON body field ``manager_key``. Reading the body here caches it for the endpoint."""
    key = request.path_params.get("manager_key") or request.query_params.get("manager_key")
    if key:
        return str(key)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and "json" in request.headers.get(
        "content-type", ""
    ):
        try:
            body = await request.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("manager_key"), str):
            return body["manager_key"]
    return None


def require_owner(principal: Principal, users: UserStore | None, manager_key: str) -> None:
    """Server-side ownership check for anything that names a manager key."""
    if principal.is_operator:
        return
    if principal.kind != "user" or principal.user_id is None:
        raise AuthError(401, "sign in to use manager data", "session_required")
    if users is None or not users.owns(principal.user_id, manager_key):
        raise AuthError(403, "this manager does not belong to your account", "not_owner")


def require_user(principal: Principal) -> Principal:
    if principal.kind != "user":
        raise AuthError(401, "sign in required", "session_required")
    return principal


def require_operator(principal: Principal) -> None:
    if not principal.is_operator:
        raise AuthError(403, "operator credentials required", "operator_required")
