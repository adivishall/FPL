"""Ephemeral PostgreSQL clusters for integration tests and local development.

Resolution order used by tests:
1. ``$FPL_TEST_DATABASE_URL`` (CI provides a Postgres service container);
2. a throw-away cluster started from local PostgreSQL binaries (``initdb``/``pg_ctl``);
3. otherwise integration tests are skipped with an explicit reason.
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path


def _find_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidates = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"), reverse=True)
    return candidates[0] if candidates else None


def postgres_binaries_available() -> bool:
    return all(_find_bin(b) for b in ("initdb", "pg_ctl", "createdb"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run(cmd: list[str], as_postgres: bool) -> None:
    if as_postgres:
        cmd = ["runuser", "-u", "postgres", "--", *cmd]
    subprocess.run(cmd, check=True, capture_output=True, text=True)  # noqa: S603


@contextlib.contextmanager
def ephemeral_postgres(db_name: str = "fpl") -> Iterator[str]:
    """Start a temporary PostgreSQL cluster; yield a SQLAlchemy URL; tear it down."""
    initdb, pg_ctl, createdb = (_find_bin(b) for b in ("initdb", "pg_ctl", "createdb"))
    if not (initdb and pg_ctl and createdb):
        raise RuntimeError("PostgreSQL binaries not found")
    as_postgres = os.geteuid() == 0  # initdb refuses to run as root
    tmp = Path(tempfile.mkdtemp(prefix="fpl-pg-"))
    data, sock = tmp / "data", tmp / "sock"
    sock.mkdir()
    if as_postgres:
        shutil.chown(tmp, "postgres")
        shutil.chown(sock, "postgres")
    port = _free_port()
    try:
        _run(
            [initdb, "-D", str(data), "-U", "fpl", "--auth=trust", "-E", "UTF8", "--no-sync"],
            as_postgres,
        )
        opts = (
            f"-p {port} -k {sock} -c listen_addresses=127.0.0.1 -c fsync=off "
            "-c synchronous_commit=off -c full_page_writes=off"
        )
        _run(
            [pg_ctl, "-D", str(data), "-o", opts, "-l", str(tmp / "pg.log"), "-w", "start"],
            as_postgres,
        )
        _run([createdb, "-h", "127.0.0.1", "-p", str(port), "-U", "fpl", db_name], as_postgres)
        yield f"postgresql+psycopg://fpl@127.0.0.1:{port}/{db_name}"
    finally:
        with contextlib.suppress(Exception):
            _run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], as_postgres)
        shutil.rmtree(tmp, ignore_errors=True)
