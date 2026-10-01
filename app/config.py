"""
Application configuration using pydantic-settings.

Precedence (highest → lowest):
  1. DATABASE_URL   – any SQLAlchemy-compatible URL
  2. CHAT_DB        – legacy: absolute or relative path to a SQLite file
  3. Default        – <project_root>/chat.db
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str | None = None
    """Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host/db"""

    chat_db: str | None = None
    """Legacy: path to SQLite file. Ignored when DATABASE_URL is set."""

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str | None = None
    """Redis connection URL, e.g. redis://localhost:6379/0"""

    redis_required: bool = False
    """If true, startup fails when Redis is unreachable."""

    # ── Admin ─────────────────────────────────────────────────────────────────
    admin_token: str | None = None
    """Secret token for admin endpoints. Unset → admin returns 503."""

    # ── Application ───────────────────────────────────────────────────────────
    environment: str = "development"
    sql_echo: bool = False
    port: int = 8000

    # ── Derived property ──────────────────────────────────────────────────────

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.chat_db:
            path = Path(self.chat_db).resolve()
            # Ensure parent directory exists so create_engine doesn't fail.
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{path}"
        default = _ROOT / "chat.db"
        return f"sqlite:///{default}"

    def __repr__(self) -> str:  # never log secrets
        return (
            f"Settings(environment={self.environment!r}, "
            f"has_database_url={self.database_url is not None}, "
            f"has_redis_url={self.redis_url is not None}, "
            f"has_admin_token={self.admin_token is not None})"
        )
