"""
Optional Redis connection management.

When REDIS_URL is not configured the application runs in single-instance mode
with in-memory rate limiting and in-process WebSocket delivery.  Nothing breaks;
distributed features are simply unavailable.

When REDIS_URL is set and REDIS_REQUIRED=true, startup fails if Redis is
unreachable.  When REDIS_REQUIRED=false (default) the application logs a warning
and continues in single-instance mode.

Key namespace: whisper:{environment}:...
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

_redis_client: "Redis | None" = None


def get_client() -> "Redis | None":
    return _redis_client


def init_redis(redis_url: str, environment: str, required: bool = False) -> "Redis | None":
    global _redis_client
    try:
        import redis as redis_lib

        client = redis_lib.from_url(redis_url, decode_responses=True, socket_connect_timeout=3)
        client.ping()
        _redis_client = client
        logger.info("Redis connected (environment=%s)", environment)
        return client
    except Exception as exc:
        if required:
            raise RuntimeError(
                f"REDIS_REQUIRED=true but Redis is unavailable: {exc}"
            ) from exc
        logger.warning(
            "Redis unavailable (%s) — running in single-instance mode (no distributed rate limiting or pub/sub)",
            exc,
        )
        _redis_client = None
        return None


def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.close()
        except Exception:
            pass
        _redis_client = None


def check_redis() -> str:
    """Returns 'up', 'not_configured', or 'down'."""
    if _redis_client is None:
        return "not_configured"
    try:
        _redis_client.ping()
        return "up"
    except Exception:
        return "down"


def make_key(environment: str, *parts: str) -> str:
    """Build a namespaced key without embedding secrets."""
    return "whisper:" + environment + ":" + ":".join(parts)
