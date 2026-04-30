"""Database configuration helpers for the control-fabric runtime.

The database layer is implementation infrastructure only. It stores fabric-local
graph, receipt, and ledger records without becoming an authority source.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


DATABASE_URL_ENV = "WGCF_DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://wgcf:wgcf@localhost:5432/wgcf"


@dataclass(frozen=True)
class DatabaseSettings:
    """Resolved database settings safe for compact operator status output."""

    url: str
    env_var: str = DATABASE_URL_ENV

    @property
    def configured_from_env(self) -> bool:
        return self.env_var in environ

    @property
    def safe_url(self) -> str:
        return redact_database_url(self.url)

    def to_status(self) -> dict[str, Any]:
        return {
            "env_var": self.env_var,
            "configured_from_env": self.configured_from_env,
            "safe_url": self.safe_url,
        }


def database_settings(database_url: str | None = None) -> DatabaseSettings:
    """Resolve database settings without opening a connection."""

    return DatabaseSettings(url=database_url or environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL))


def redact_database_url(database_url: str) -> str:
    """Return a URL safe for logs, receipts, and operator status output."""

    parsed = urlsplit(database_url)
    if not parsed.password:
        return database_url
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:***@" if username else "***@"
    return urlunsplit((parsed.scheme, f"{auth}{hostname}{port}", parsed.path, parsed.query, parsed.fragment))


def create_database_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine for runtime code and migrations."""

    settings = database_settings(database_url)
    return create_engine(settings.url, pool_pre_ping=True)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """Create a session factory bound to the configured database URL."""

    return sessionmaker(bind=create_database_engine(database_url), autoflush=False, expire_on_commit=False)
