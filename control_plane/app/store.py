from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/control-plane.sqlite3")
_url = make_url(DATABASE_URL)
_engine_kwargs = {"pool_pre_ping": True}
if _url.get_backend_name() == "sqlite":
    database_file = _url.database
    if database_file and database_file != ":memory:":
        database_path = Path(database_file)
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_mode = database_path.parent.stat().st_mode & 0o777
        if directory_mode & 0o077:
            raise RuntimeError("SQLite data directory must be private (mode 0700)")
        if database_path.exists():
            os.chmod(database_path, 0o600)
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragmas(connection, _record):
    if engine.dialect.name == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def initialize() -> None:
    default_auto_create = "true" if engine.dialect.name == "sqlite" else "false"
    if os.environ.get("AUTO_CREATE_SCHEMA", default_auto_create).lower() == "true":
        Base.metadata.create_all(engine)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
