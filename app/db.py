"""
SQLAlchemy engine factory, session factory, declarative Base, and ISODateTime type.

Usage:
    engine = build_engine(settings.effective_database_url, echo=settings.sql_echo)
    SessionLocal = build_session_factory(engine)

The module-level engine and SessionLocal are created in app/main.py so that
importlib.reload(app.main) picks up a fresh CHAT_DB / DATABASE_URL env var in
tests without requiring a reload of this module.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Generator

from sqlalchemy import DateTime, String, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)


# ── Custom datetime type ──────────────────────────────────────────────────────

class ISODateTime(TypeDecorator):
    """
    Portable timezone-aware datetime:
      - SQLite  → TEXT column, ISO-8601 strings (compatible with existing data)
      - PostgreSQL → TIMESTAMPTZ column
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(50))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            if isinstance(value, str):
                return value
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                return value.isoformat()
            return str(value)
        # PostgreSQL: pass datetime objects through; convert strings to datetime
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return value
        return value


# ── Declarative base ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Engine factory ────────────────────────────────────────────────────────────

def build_engine(database_url: str, echo: bool = False):
    """
    Create a SQLAlchemy engine appropriate for the given URL.

    - SQLite: check_same_thread=False, foreign_keys=ON pragma
    - PostgreSQL: default connection pool settings
    """
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(database_url, connect_args=connect_args, echo=echo)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_session_factory(engine) -> sessionmaker:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


# ── Health helper ─────────────────────────────────────────────────────────────

def check_database(engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False
