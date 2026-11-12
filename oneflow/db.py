"""Database connection. PostgreSQL only.

Set DATABASE_URL to use your own server. Without it a private PostgreSQL is
started from the `pgserver` package in ./.pgdata, so the app runs on a machine
with no database installed.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

EMBEDDED_DATABASE = "oneflow_v1"   # no migrations yet: a schema change gets a new name
_engine: Engine | None = None
_embedded = None                   # keeps the pgserver handle alive


def _url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def start_embedded(pgdata: str | Path) -> str:
    global _embedded
    import pgserver
    _embedded = pgserver.get_server(Path(pgdata).resolve())
    admin = create_engine(_url(_embedded.get_uri()), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": EMBEDDED_DATABASE}):
            conn.execute(text(f'CREATE DATABASE "{EMBEDDED_DATABASE}"'))
    admin.dispose()
    return _url(_embedded.get_uri(database=EMBEDDED_DATABASE))


def set_engine(engine: Engine) -> None:
    global _engine
    _engine = engine
    Base.metadata.create_all(engine)


def get_engine() -> Engine:
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        url = _url(url) if url else start_embedded(os.environ.get("ONEFLOW_PGDATA", ".pgdata"))
        set_engine(create_engine(url, pool_pre_ping=True))
    return _engine


def new_session() -> Session:
    return sessionmaker(get_engine(), expire_on_commit=False)()


def get_session() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
    finally:
        session.close()
