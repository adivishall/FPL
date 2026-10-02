"""Engine and session management."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

ENV_DATABASE_URL = "FPL_DATABASE_URL"


def database_url() -> str:
    url = os.environ.get(ENV_DATABASE_URL)
    if not url:
        raise RuntimeError(
            f"{ENV_DATABASE_URL} is not set (e.g. postgresql+psycopg://fpl:fpl@localhost:5432/fpl)"
        )
    return url


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    return create_engine(
        url or database_url(),
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


@lru_cache(maxsize=8)
def _cached_engine(url: str) -> Engine:
    return make_engine(url)


def get_engine(url: str | None = None) -> Engine:
    return _cached_engine(url or database_url())


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False
