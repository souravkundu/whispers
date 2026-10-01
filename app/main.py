"""
Whisper — FastAPI application.

This module is the composition root.  Module-level objects (engine, SessionLocal,
manager, _session_rate_limiter) are computed from environment variables so that
``importlib.reload(app.main)`` picks up a fresh CHAT_DB / DATABASE_URL value,
which is how the test suite isolates each test in a temporary SQLite database.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, build_engine, build_session_factory, check_database
import app.repositories.users as users_repo
import app.repositories.contacts as contacts_repo
import app.repositories.messages as messages_repo
import app.repositories.blocks as blocks_repo
import app.repositories.reports as reports_repo
import app.repositories.rooms as rooms_repo
from app import redis_client as _redis

# Import models so Base.metadata is populated for create_all
from app.models import (  # noqa: F401
    User as _UserModel,
    Contact, Conversation, Message, BlockedUser, Report, Room, RoomMember, RoomMessage,
)

logger = logging.getLogger(__name__)

# ── Module-level singletons (recreated on importlib.reload) ───────────────────

_settings = Settings()

engine = build_engine(_settings.effective_database_url, echo=_settings.sql_echo)
SessionLocal = build_session_factory(engine)

DEFAULT_EXPIRY_HOURS = 24
VALID_REPORT_REASONS = frozenset({"spam", "harassment", "abuse", "impersonation", "other"})

ROOT = Path(__file__).resolve().parent.parent
STATIC_PATH = ROOT / "static"


# ── Rate limiter ──────────────────────────────────────────────────────────────

class _InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter (single-instance mode)."""

    def __init__(self, max_calls: int, period: float) -> None:
        self.max_calls = max_calls
        self.period = period
        self._calls: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.period
        with self._lock:
            calls = self._calls[key]
            while calls and calls[0] < cutoff:
                calls.pop(0)
            if len(calls) >= self.max_calls:
                return False
            calls.append(now)
            return True


_session_rate_limiter = _InMemoryRateLimiter(max_calls=10, period=60.0)


def _rate_limit_check(client_ip: str) -> bool:
    redis = _redis.get_client()
    if redis is not None:
        env = _settings.environment
        key = _redis.make_key(env, "ratelimit", "session", client_ip)
        try:
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)
            count, _ = pipe.execute()
            return count <= 10
        except Exception:
            logger.warning("Redis rate-limit check failed; falling back to in-memory")
    return _session_rate_limiter.is_allowed(client_ip)


# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)
        _presence_refresh(user_id)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        sockets = self.connections.get(user_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                self.connections.pop(user_id, None)
        _presence_remove(user_id)

    async def notify(self, user_id: int, payload: dict) -> None:
        for websocket in list(self.connections.get(user_id, set())):
            try:
                await websocket.send_json(payload)
            except Exception:
                pass

    async def notify_room(self, user_ids: list[int], payload: dict) -> None:
        for uid in user_ids:
            await self.notify(uid, payload)


manager = ConnectionManager()


# ── Presence helpers ──────────────────────────────────────────────────────────

_PRESENCE_TTL = 90


def _presence_refresh(user_id: int) -> None:
    redis = _redis.get_client()
    if redis is None:
        return
    try:
        key = _redis.make_key(_settings.environment, "presence", "user", str(user_id))
        redis.setex(key, _PRESENCE_TTL, "1")
    except Exception:
        pass


def _presence_remove(user_id: int) -> None:
    redis = _redis.get_client()
    if redis is None:
        return
    try:
        key = _redis.make_key(_settings.environment, "presence", "user", str(user_id))
        redis.delete(key)
    except Exception:
        pass


# ── Redis Pub/Sub distribution ────────────────────────────────────────────────

_PUBSUB_CHANNEL = "whisper:events"
_pubsub_task: asyncio.Task | None = None


async def _redis_subscriber_loop() -> None:
    import json as _json

    redis = _redis.get_client()
    if redis is None:
        return
    try:
        ps = redis.pubsub()
        ps.subscribe(_PUBSUB_CHANNEL)
        while True:
            msg = ps.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if msg and msg["type"] == "message":
                try:
                    event = _json.loads(msg["data"])
                    etype = event.get("type")
                    if etype == "message":
                        payload = event["payload"]
                        recipient_id = event.get("recipient_id")
                        if recipient_id is not None:
                            await manager.notify(recipient_id, {"type": "message", "message": payload})
                    elif etype == "room_message":
                        payload = event["payload"]
                        room_member_ids = event.get("member_ids", [])
                        sender_id = event.get("sender_id")
                        for uid in room_member_ids:
                            if uid == sender_id:
                                continue
                            await manager.notify(uid, {"type": "room_message", "message": payload})
                except Exception:
                    pass
            await asyncio.sleep(0)
    except Exception as exc:
        logger.warning("Redis subscriber loop ended: %s", exc)


def _publish_event(event: dict) -> None:
    import json as _json

    redis = _redis.get_client()
    if redis is None:
        return
    try:
        redis.publish(_PUBSUB_CHANNEL, _json.dumps(event))
    except Exception as exc:
        logger.warning("Redis publish failed: %s", exc)


# ── Cleanup ───────────────────────────────────────────────────────────────────

_CLEANUP_LOCK_KEY = "whisper:cleanup:lock"
_CLEANUP_LOCK_TTL = 120


def purge_expired_messages() -> None:
    redis = _redis.get_client()
    lock_acquired = False
    lock_val = secrets.token_hex(8)

    if redis is not None:
        try:
            lock_acquired = bool(
                redis.set(_CLEANUP_LOCK_KEY, lock_val, nx=True, ex=_CLEANUP_LOCK_TTL)
            )
            if not lock_acquired:
                return
        except Exception:
            pass

    try:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            dm_count = messages_repo.purge_expired(db, now)
            rm_count = rooms_repo.purge_expired_messages(db, now)
            rr_count = rooms_repo.purge_expired_rooms(db, now)
            db.commit()
        logger.debug(
            "Cleanup: %d direct messages, %d room messages, %d rooms purged",
            dm_count, rm_count, rr_count,
        )
    finally:
        if redis is not None and lock_acquired:
            try:
                stored = redis.get(_CLEANUP_LOCK_KEY)
                if stored == lock_val:
                    redis.delete(_CLEANUP_LOCK_KEY)
            except Exception:
                pass


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        purge_expired_messages()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pubsub_task

    Base.metadata.create_all(engine)

    if _settings.redis_url:
        _redis.init_redis(
            _settings.redis_url,
            _settings.environment,
            required=_settings.redis_required,
        )
        if _redis.get_client() is not None:
            _pubsub_task = asyncio.create_task(_redis_subscriber_loop())
    else:
        logger.info("REDIS_URL not set — running in single-instance mode")

    cleanup_task = asyncio.create_task(cleanup_loop())
    yield

    cleanup_task.cancel()
    if _pubsub_task is not None:
        _pubsub_task.cancel()
    _redis.close_redis()


app = FastAPI(title="Whisperss", lifespan=lifespan)


# ── Database dependency ───────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


# ── Request body models ───────────────────────────────────────────────────────

class UsernameBody(BaseModel):
    username: str = Field(min_length=3, max_length=24, pattern=r"^[A-Za-z0-9_]+$")

    @field_validator("username")
    @classmethod
    def normalize_username(cls, username: str) -> str:
        return username.strip()


class SessionBody(UsernameBody):
    password: str | None = Field(default=None, min_length=8, max_length=128)


class ReservationBody(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class MessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, body: str) -> str:
        if not body.strip():
            raise ValueError("Message cannot be empty")
        return body.strip()


class ExpiryBody(BaseModel):
    expiry_hours: int = Field(ge=1, le=24 * 30)


class ReportBody(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    reason: str
    details: str | None = Field(default=None, max_length=1000)
    message_id: int | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, reason: str) -> str:
        if reason not in VALID_REPORT_REASONS:
            raise ValueError(f"reason must be one of: {', '.join(sorted(VALID_REPORT_REASONS))}")
        return reason


class CreateRoomBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 30 * 6)
    default_message_lifetime_hours: int = Field(default=24, ge=1, le=24 * 30)


class JoinRoomBody(BaseModel):
    room_code: str = Field(min_length=1, max_length=20)

    @field_validator("room_code")
    @classmethod
    def normalize_room_code(cls, code: str) -> str:
        return code.strip().upper()


class ReportStatusBody(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, status: str) -> str:
        if status not in {"open", "reviewed", "dismissed"}:
            raise ValueError("status must be one of: open, reviewed, dismissed")
        return status


# ── Auth dependencies ─────────────────────────────────────────────────────────

def current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Annotated[Session, Depends(get_db)] = None,
) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing session token")
    token = authorization.removeprefix("Bearer ")
    user = users_repo.get_by_token(db, token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid session token")
    if user.disabled:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return {"id": user.id, "username": user.username}


def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    admin_token = _settings.admin_token
    if not admin_token:
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    if not authorization or authorization != f"Bearer {admin_token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


User = Annotated[dict, Depends(current_user)]
Admin = Annotated[None, Depends(require_admin)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_user_or_404(db: Session, username: str):
    user = users_repo.get_by_username(db, username)
    if user is None:
        raise HTTPException(status_code=404, detail="Username not found")
    return user


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


def generate_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Session endpoints ─────────────────────────────────────────────────────────

@app.post("/api/session", status_code=201)
def create_session(request: Request, body: SessionBody, db: DB) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        raise HTTPException(
            status_code=429, detail="Too many requests. Please wait a moment and try again."
        )
    token = secrets.token_urlsafe(32)
    existing = users_repo.get_by_username(db, body.username)
    if existing is not None:
        if existing.disabled:
            raise HTTPException(status_code=403, detail="Account is disabled")
        if not existing.password_hash:
            raise HTTPException(status_code=409, detail="Username is already taken")
        if body.password is None:
            raise HTTPException(status_code=401, detail="This username is reserved; enter its password")
        candidate = hash_password(body.password, bytes.fromhex(existing.password_salt))
        if not hmac.compare_digest(candidate, existing.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect password")
        users_repo.update_token(db, existing.id, token)
        db.commit()
        return {"id": existing.id, "username": existing.username, "token": token, "reserved": True}

    try:
        user = users_repo.create(db, username=body.username, token=token, created_at=datetime.now(UTC))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username is already taken")
    return {"id": user.id, "username": user.username, "token": token, "reserved": False}


@app.get("/api/session")
def get_session(user: User, db: DB) -> dict:
    reserved = users_repo.has_password(db, user["id"])
    return {"id": user["id"], "username": user["username"], "reserved": reserved}


@app.post("/api/session/reserve")
def reserve_username(body: ReservationBody, user: User, db: DB) -> dict:
    if users_repo.has_password(db, user["id"]):
        raise HTTPException(status_code=409, detail="Username is already reserved")
    salt = secrets.token_bytes(16)
    pw_hash = hash_password(body.password, salt)
    users_repo.set_password(db, user["id"], pw_hash, salt.hex())
    db.commit()
    return {"username": user["username"], "reserved": True}


# ── User / contact endpoints ──────────────────────────────────────────────────

@app.get("/api/users/{username}")
def get_user(username: str, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, username)
    if target.id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot chat with yourself")
    return {"id": target.id, "username": target.username}


@app.get("/api/contacts")
def get_contacts(user: User, db: DB) -> list[dict]:
    return contacts_repo.list_for_user(db, user["id"])


@app.post("/api/contacts", status_code=201)
def save_contact(body: UsernameBody, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, body.username)
    if target.id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot save yourself")
    contacts_repo.add(db, user["id"], target.id, datetime.now(UTC))
    db.commit()
    return {"id": target.id, "username": target.username}


# ── Block / unblock endpoints ─────────────────────────────────────────────────

@app.post("/api/block", status_code=201)
def block_user(body: UsernameBody, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, body.username)
    if target.id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot block yourself")
    blocks_repo.block(db, user["id"], target.id, datetime.now(UTC))
    db.commit()
    return {"blocked": target.username}


@app.post("/api/unblock")
def unblock_user(body: UsernameBody, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, body.username)
    blocks_repo.unblock(db, user["id"], target.id)
    db.commit()
    return {"unblocked": target.username}


@app.get("/api/blocked")
def get_blocked(user: User, db: DB) -> list[dict]:
    return blocks_repo.list_blocked(db, user["id"])


# ── Conversation endpoints ────────────────────────────────────────────────────

@app.post("/api/conversations", status_code=201)
def open_conversation(body: UsernameBody, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, body.username)
    if target.id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot chat with yourself")
    first_id, second_id = sorted((user["id"], target.id))
    convo = messages_repo.open_or_get_conversation(
        db,
        first_user_id=first_id,
        second_user_id=second_id,
        expiry_hours=DEFAULT_EXPIRY_HOURS,
        created_at=datetime.now(UTC),
    )
    db.commit()
    return {
        "id": convo.id,
        "expiry_hours": convo.expiry_hours,
        "with_user": {"id": target.id, "username": target.username},
    }


@app.patch("/api/conversations/{conversation_id}")
def update_expiry(conversation_id: int, body: ExpiryBody, user: User, db: DB) -> dict:
    convo = messages_repo.get_conversation(db, user["id"], conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages_repo.update_expiry(db, conversation_id, body.expiry_hours)
    db.commit()
    return {"id": conversation_id, "expiry_hours": body.expiry_hours}


@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int, user: User, db: DB) -> list[dict]:
    purge_expired_messages()
    convo = messages_repo.get_conversation(db, user["id"], conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return messages_repo.list_messages(db, conversation_id)


@app.post("/api/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: int, body: MessageBody, user: User, db: DB) -> dict:
    convo = messages_repo.get_conversation(db, user["id"], conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    recipient_id = (
        convo.second_user_id if convo.first_user_id == user["id"] else convo.first_user_id
    )
    if blocks_repo.is_blocked(db, recipient_id, user["id"]):
        raise HTTPException(status_code=403, detail="You cannot send messages to this user")
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(hours=convo.expiry_hours)
    msg = messages_repo.create_message(
        db,
        conversation_id=conversation_id,
        sender_id=user["id"],
        body=body.body,
        created_at=created_at,
        expires_at=expires_at,
    )
    db.commit()
    payload = {
        "id": msg.id,
        "conversation_id": conversation_id,
        "sender": user["username"],
        "body": body.body,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    if _redis.get_client() is not None:
        _publish_event({
            "type": "message",
            "recipient_id": recipient_id,
            "sender_id": user["id"],
            "payload": payload,
        })
    else:
        await manager.notify(recipient_id, {"type": "message", "message": payload})
    return payload


# ── Report endpoint ───────────────────────────────────────────────────────────

@app.post("/api/report", status_code=201)
def report_user(body: ReportBody, user: User, db: DB) -> dict:
    target = _find_user_or_404(db, body.username)
    if target.id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot report yourself")
    if body.message_id is not None:
        msg = messages_repo.get_message_by_id_and_sender(db, body.message_id, target.id)
        if msg is None:
            raise HTTPException(status_code=404, detail="Message not found for that user")
    reports_repo.create(
        db,
        reporter_id=user["id"],
        reported_id=target.id,
        message_id=body.message_id,
        reason=body.reason,
        details=body.details,
        created_at=datetime.now(UTC),
    )
    db.commit()
    return {"reported": target.username, "reason": body.reason}


# ── Room endpoints ────────────────────────────────────────────────────────────

@app.post("/api/rooms", status_code=201)
def create_room(body: CreateRoomBody, user: User, db: DB) -> dict:
    expires_at = None
    if body.expires_in_hours:
        expires_at = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)
    lifetime_seconds = body.default_message_lifetime_hours * 3600
    for _ in range(5):
        room_code = generate_room_code()
        if rooms_repo.get_by_code(db, room_code) is None:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate a unique room code")
    room = rooms_repo.create_room(
        db,
        room_code=room_code,
        display_name=body.display_name,
        created_by_user_id=user["id"],
        created_at=datetime.now(UTC),
        expires_at=expires_at,
        default_message_lifetime_seconds=lifetime_seconds,
    )
    alias = rooms_repo.assign_alias(db, room.id)
    rooms_repo.add_member(db, room.id, user["id"], alias, datetime.now(UTC))
    db.commit()
    return {
        "room_code": room_code,
        "display_name": body.display_name,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "default_message_lifetime_hours": body.default_message_lifetime_hours,
        "your_alias": alias,
    }


@app.get("/api/rooms")
def list_rooms(user: User, db: DB) -> list[dict]:
    return rooms_repo.list_for_user(db, user["id"])


@app.post("/api/rooms/join", status_code=201)
def join_room(body: JoinRoomBody, user: User, db: DB) -> dict:
    room = rooms_repo.get_by_code(db, body.room_code)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.expires_at and room.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="This room has expired")
    existing = rooms_repo.get_membership(db, room.id, user["id"])
    if existing:
        return {
            "room_code": room.room_code,
            "display_name": room.display_name,
            "your_alias": existing.alias,
            "already_member": True,
        }
    alias = rooms_repo.assign_alias(db, room.id)
    rooms_repo.add_member(db, room.id, user["id"], alias, datetime.now(UTC))
    db.commit()
    return {
        "room_code": room.room_code,
        "display_name": room.display_name,
        "your_alias": alias,
        "already_member": False,
    }


@app.get("/api/rooms/{room_code}/messages")
def get_room_messages(room_code: str, user: User, db: DB) -> list[dict]:
    purge_expired_messages()
    room = rooms_repo.get_by_code(db, room_code)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    membership = rooms_repo.get_membership(db, room.id, user["id"])
    if membership is None:
        raise HTTPException(status_code=403, detail="You are not a member of this room")
    return rooms_repo.list_messages(db, room.id, user["id"])


@app.post("/api/rooms/{room_code}/messages", status_code=201)
async def send_room_message(room_code: str, body: MessageBody, user: User, db: DB) -> dict:
    room = rooms_repo.get_by_code(db, room_code)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.expires_at and room.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="This room has expired")
    membership = rooms_repo.get_membership(db, room.id, user["id"])
    if membership is None:
        raise HTTPException(status_code=403, detail="You are not a member of this room")
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(seconds=room.default_message_lifetime_seconds)
    msg = rooms_repo.create_message(
        db,
        room_id=room.id,
        sender_id=user["id"],
        body=body.body,
        created_at=created_at,
        expires_at=expires_at,
    )
    member_ids = rooms_repo.list_member_ids(db, room.id)
    sender_alias = membership.alias
    db.commit()

    ws_payload = {
        "id": msg.id,
        "room_id": room.id,
        "room_code": room_code.upper(),
        "sender_alias": sender_alias,
        "body": body.body,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "is_mine": False,
    }
    if _redis.get_client() is not None:
        _publish_event({
            "type": "room_message",
            "sender_id": user["id"],
            "member_ids": member_ids,
            "payload": ws_payload,
        })
    else:
        for uid in member_ids:
            if uid != user["id"]:
                await manager.notify(uid, {"type": "room_message", "message": ws_payload})

    return {**ws_payload, "is_mine": True}


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.get("/api/admin/reports")
def admin_list_reports(_: Admin, db: DB) -> list[dict]:
    return reports_repo.list_all(db)


@app.post("/api/admin/reports/{report_id}/status")
def admin_update_report_status(report_id: int, body: ReportStatusBody, _: Admin, db: DB) -> dict:
    report = reports_repo.get_by_id(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    reports_repo.update_status(db, report_id, body.status)
    db.commit()
    return {"id": report_id, "status": body.status}


@app.get("/api/admin/users")
def admin_list_users(_: Admin, db: DB) -> list[dict]:
    return [
        {
            "id": u.id,
            "username": u.username,
            "created_at": u.created_at.isoformat() if hasattr(u.created_at, "isoformat") else u.created_at,
            "disabled": bool(u.disabled),
        }
        for u in users_repo.list_all(db)
    ]


@app.post("/api/admin/users/{username}/disable")
def admin_disable_user(username: str, _: Admin, db: DB) -> dict:
    user = users_repo.disable(db, username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    db.commit()
    return {"username": username, "disabled": True}


@app.post("/api/admin/users/{username}/enable")
def admin_enable_user(username: str, _: Admin, db: DB) -> dict:
    user = users_repo.enable(db, username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    db.commit()
    return {"username": username, "disabled": False}


# ── Health endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/dependencies")
def health_dependencies() -> dict:
    db_status = "up" if check_database(engine) else "down"
    redis_status = _redis.check_redis()
    result: dict = {"status": "ok", "database": db_status, "redis": redis_status}
    if redis_status == "not_configured":
        result["mode"] = "single_instance"
    if db_status == "down":
        result["status"] = "degraded"
    return JSONResponse(
        status_code=200 if result["status"] == "ok" else 503,
        content=result,
    )


@app.get("/ready")
def readiness() -> dict:
    db_ok = check_database(engine)
    redis_status = _redis.check_redis()
    redis_ok = redis_status in ("up", "not_configured")
    if _settings.redis_required and redis_status != "up":
        redis_ok = False
    if not db_ok or not redis_ok:
        raise HTTPException(
            status_code=503,
            detail={"database": "up" if db_ok else "down", "redis": redis_status},
        )
    return {"status": "ready"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str) -> None:
    with SessionLocal() as db:
        user = users_repo.get_by_token(db, token)
    if user is None:
        await websocket.close(code=1008)
        return
    user_id = user.id
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)


# ── Static files ──────────────────────────────────────────────────────────────

if STATIC_PATH.exists():
    app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_PATH / "index.html")
