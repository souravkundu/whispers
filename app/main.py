from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import random
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = Path(os.getenv("CHAT_DB", ROOT / "chat.db"))
DEFAULT_EXPIRY_HOURS = 24

VALID_REPORT_REASONS = frozenset({"spam", "harassment", "abuse", "impersonation", "other"})

_ALIAS_ADJ = [
    "Quiet", "Blue", "Silver", "Hidden", "Swift", "Amber", "Crimson", "Jade",
    "Misty", "Teal", "Violet", "Golden", "Dark", "Pale", "Bold", "Soft",
    "Gray", "Bright", "Calm", "Wild",
]
_ALIAS_NOUN = [
    "River", "Fox", "Moon", "Panda", "Wolf", "Hawk", "Pine", "Stone",
    "Vale", "Sage", "Otter", "Bear", "Raven", "Deer", "Eagle", "Heron",
    "Lynx", "Owl", "Wren", "Finch",
]


class _InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter."""

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
            # drop timestamps outside the window
            while calls and calls[0] < cutoff:
                calls.pop(0)
            if len(calls) >= self.max_calls:
                return False
            calls.append(now)
            return True


_session_rate_limiter = _InMemoryRateLimiter(max_calls=10, period=60.0)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contacts (
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                contact_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, contact_id),
                CHECK (owner_id != contact_id)
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                second_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expiry_hours INTEGER NOT NULL DEFAULT 24,
                created_at TEXT NOT NULL,
                UNIQUE (first_user_id, second_user_id),
                CHECK (first_user_id < second_user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_expiry_idx ON messages(expires_at);
            CREATE TABLE IF NOT EXISTS blocked_users (
                blocker_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                blocked_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (blocker_user_id, blocked_user_id),
                CHECK (blocker_user_id != blocked_user_id)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reported_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                reason TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                default_message_lifetime_seconds INTEGER NOT NULL DEFAULT 86400
            );
            CREATE TABLE IF NOT EXISTS room_members (
                room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (room_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS room_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS room_messages_expiry_idx ON room_messages(expires_at);
            """
        )
        user_columns = {
            column["name"] for column in database.execute("PRAGMA table_info(users)").fetchall()
        }
        if "password_hash" not in user_columns:
            database.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "password_salt" not in user_columns:
            database.execute("ALTER TABLE users ADD COLUMN password_salt TEXT")
        if "disabled" not in user_columns:
            database.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")


def purge_expired_messages() -> None:
    ts = now_iso()
    with connect() as database:
        database.execute("DELETE FROM messages WHERE expires_at <= ?", (ts,))
        database.execute("DELETE FROM room_messages WHERE expires_at <= ?", (ts,))
        database.execute("DELETE FROM rooms WHERE expires_at IS NOT NULL AND expires_at <= ?", (ts,))


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        purge_expired_messages()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    cleanup_task = asyncio.create_task(cleanup_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(title="Whisper", lifespan=lifespan)


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


def current_user(authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing session token")
    token = authorization.removeprefix("Bearer ")
    with connect() as database:
        user = database.execute(
            "SELECT id, username, token, disabled FROM users WHERE token = ?", (token,)
        ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid session token")
    if user["disabled"]:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return user


def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    token = os.getenv("ADMIN_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    if not authorization or authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


User = Annotated[sqlite3.Row, Depends(current_user)]
Admin = Annotated[None, Depends(require_admin)]


def find_user(database: sqlite3.Connection, username: str) -> sqlite3.Row:
    user = database.execute(
        "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if user is None:
        raise HTTPException(status_code=404, detail="Username not found")
    return user


def conversation_for(database: sqlite3.Connection, user_id: int, conversation_id: int) -> sqlite3.Row:
    conversation = database.execute(
        """
        SELECT * FROM conversations
        WHERE id = ? AND (first_user_id = ? OR second_user_id = ?)
        """,
        (conversation_id, user_id, user_id),
    ).fetchone()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def is_blocked(database: sqlite3.Connection, blocker_id: int, blocked_id: int) -> bool:
    return database.execute(
        "SELECT 1 FROM blocked_users WHERE blocker_user_id = ? AND blocked_user_id = ?",
        (blocker_id, blocked_id),
    ).fetchone() is not None


def generate_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def assign_room_alias(database: sqlite3.Connection, room_id: int) -> str:
    existing = {
        row["alias"]
        for row in database.execute(
            "SELECT alias FROM room_members WHERE room_id = ?", (room_id,)
        ).fetchall()
    }
    for _ in range(400):
        alias = f"{random.choice(_ALIAS_ADJ)}{random.choice(_ALIAS_NOUN)}"
        if alias not in existing:
            return alias
    return f"{random.choice(_ALIAS_ADJ)}{random.choice(_ALIAS_NOUN)}{random.randint(2, 99)}"


def get_room(database: sqlite3.Connection, room_code: str) -> sqlite3.Row:
    room = database.execute(
        "SELECT * FROM rooms WHERE room_code = ?", (room_code.upper(),)
    ).fetchone()
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


@app.post("/api/session", status_code=201)
def create_session(request: Request, body: SessionBody) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not _session_rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")
    token = secrets.token_urlsafe(32)
    with connect() as database:
        existing = database.execute(
            """
            SELECT id, username, password_hash, password_salt, disabled
            FROM users WHERE username = ? COLLATE NOCASE
            """,
            (body.username,),
        ).fetchone()
        if existing is not None:
            if existing["disabled"]:
                raise HTTPException(status_code=403, detail="Account is disabled")
            if not existing["password_hash"]:
                raise HTTPException(status_code=409, detail="Username is already taken")
            if body.password is None:
                raise HTTPException(status_code=401, detail="This username is reserved; enter its password")
            candidate = hash_password(body.password, bytes.fromhex(existing["password_salt"]))
            if not hmac.compare_digest(candidate, existing["password_hash"]):
                raise HTTPException(status_code=401, detail="Incorrect password")
            database.execute("UPDATE users SET token = ? WHERE id = ?", (token, existing["id"]))
            return {
                "id": existing["id"],
                "username": existing["username"],
                "token": token,
                "reserved": True,
            }

        try:
            cursor = database.execute(
                "INSERT INTO users(username, token, created_at) VALUES (?, ?, ?)",
                (body.username, token, now_iso()),
            )
            user_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="Username is already taken") from error
    return {"id": user_id, "username": body.username, "token": token, "reserved": False}


@app.get("/api/session")
def get_session(user: User) -> dict:
    with connect() as database:
        reserved = database.execute(
            "SELECT password_hash IS NOT NULL AS reserved FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["reserved"]
    return {"id": user["id"], "username": user["username"], "reserved": bool(reserved)}


@app.post("/api/session/reserve")
def reserve_username(body: ReservationBody, user: User) -> dict:
    salt = secrets.token_bytes(16)
    password_hash = hash_password(body.password, salt)
    with connect() as database:
        existing = database.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if existing["password_hash"]:
            raise HTTPException(status_code=409, detail="Username is already reserved")
        database.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (password_hash, salt.hex(), user["id"]),
        )
    return {"username": user["username"], "reserved": True}


@app.get("/api/users/{username}")
def get_user(username: str, user: User) -> dict:
    with connect() as database:
        target = find_user(database, username)
    if target["id"] == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot chat with yourself")
    return {"id": target["id"], "username": target["username"]}


@app.get("/api/contacts")
def get_contacts(user: User) -> list[dict]:
    with connect() as database:
        contacts = database.execute(
            """
            SELECT users.id, users.username FROM contacts
            JOIN users ON users.id = contacts.contact_id
            WHERE contacts.owner_id = ? ORDER BY users.username COLLATE NOCASE
            """,
            (user["id"],),
        ).fetchall()
    return [dict(contact) for contact in contacts]


@app.post("/api/contacts", status_code=201)
def save_contact(body: UsernameBody, user: User) -> dict:
    with connect() as database:
        target = find_user(database, body.username)
        if target["id"] == user["id"]:
            raise HTTPException(status_code=400, detail="You cannot save yourself")
        database.execute(
            "INSERT OR IGNORE INTO contacts(owner_id, contact_id, created_at) VALUES (?, ?, ?)",
            (user["id"], target["id"], now_iso()),
        )
    return dict(target)


@app.post("/api/block", status_code=201)
def block_user(body: UsernameBody, user: User) -> dict:
    with connect() as database:
        target = find_user(database, body.username)
        if target["id"] == user["id"]:
            raise HTTPException(status_code=400, detail="You cannot block yourself")
        try:
            database.execute(
                "INSERT INTO blocked_users(blocker_user_id, blocked_user_id, created_at) VALUES (?, ?, ?)",
                (user["id"], target["id"], now_iso()),
            )
        except sqlite3.IntegrityError:
            pass  # already blocked
    return {"blocked": target["username"]}


@app.post("/api/unblock")
def unblock_user(body: UsernameBody, user: User) -> dict:
    with connect() as database:
        target = find_user(database, body.username)
        database.execute(
            "DELETE FROM blocked_users WHERE blocker_user_id = ? AND blocked_user_id = ?",
            (user["id"], target["id"]),
        )
    return {"unblocked": target["username"]}


@app.get("/api/blocked")
def get_blocked(user: User) -> list[dict]:
    with connect() as database:
        rows = database.execute(
            """
            SELECT users.id, users.username FROM blocked_users
            JOIN users ON users.id = blocked_users.blocked_user_id
            WHERE blocked_users.blocker_user_id = ?
            ORDER BY users.username COLLATE NOCASE
            """,
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/conversations", status_code=201)
def open_conversation(body: UsernameBody, user: User) -> dict:
    with connect() as database:
        target = find_user(database, body.username)
        if target["id"] == user["id"]:
            raise HTTPException(status_code=400, detail="You cannot chat with yourself")
        first_id, second_id = sorted((user["id"], target["id"]))
        database.execute(
            """
            INSERT OR IGNORE INTO conversations
                (first_user_id, second_user_id, expiry_hours, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (first_id, second_id, DEFAULT_EXPIRY_HOURS, now_iso()),
        )
        conversation = database.execute(
            """
            SELECT id, expiry_hours FROM conversations
            WHERE first_user_id = ? AND second_user_id = ?
            """,
            (first_id, second_id),
        ).fetchone()
    return {
        "id": conversation["id"],
        "expiry_hours": conversation["expiry_hours"],
        "with_user": dict(target),
    }


@app.patch("/api/conversations/{conversation_id}")
def update_expiry(conversation_id: int, body: ExpiryBody, user: User) -> dict:
    with connect() as database:
        conversation_for(database, user["id"], conversation_id)
        database.execute(
            "UPDATE conversations SET expiry_hours = ? WHERE id = ?",
            (body.expiry_hours, conversation_id),
        )
    return {"id": conversation_id, "expiry_hours": body.expiry_hours}


@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int, user: User) -> list[dict]:
    purge_expired_messages()
    with connect() as database:
        conversation_for(database, user["id"], conversation_id)
        messages = database.execute(
            """
            SELECT messages.id, messages.body, messages.created_at, messages.expires_at,
                   users.username AS sender
            FROM messages JOIN users ON users.id = messages.sender_id
            WHERE conversation_id = ? ORDER BY messages.created_at
            """,
            (conversation_id,),
        ).fetchall()
    return [dict(message) for message in messages]


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        sockets = self.connections.get(user_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                self.connections.pop(user_id, None)

    async def notify(self, user_id: int, payload: dict) -> None:
        for websocket in list(self.connections.get(user_id, set())):
            await websocket.send_json(payload)

    async def notify_room(self, user_ids: list[int], payload: dict) -> None:
        for user_id in user_ids:
            await self.notify(user_id, payload)


manager = ConnectionManager()


@app.post("/api/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: int, body: MessageBody, user: User) -> dict:
    with connect() as database:
        conversation = conversation_for(database, user["id"], conversation_id)
        recipient_id = (
            conversation["second_user_id"]
            if conversation["first_user_id"] == user["id"]
            else conversation["first_user_id"]
        )
        if is_blocked(database, recipient_id, user["id"]):
            raise HTTPException(status_code=403, detail="You cannot send messages to this user")
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(hours=conversation["expiry_hours"])
        cursor = database.execute(
            """
            INSERT INTO messages(conversation_id, sender_id, body, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, user["id"], body.body, created_at.isoformat(), expires_at.isoformat()),
        )
        message = {
            "id": cursor.lastrowid,
            "conversation_id": conversation_id,
            "sender": user["username"],
            "body": body.body,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
    await manager.notify(recipient_id, {"type": "message", "message": message})
    return message


@app.post("/api/report", status_code=201)
def report_user(body: ReportBody, user: User) -> dict:
    with connect() as database:
        target = find_user(database, body.username)
        if target["id"] == user["id"]:
            raise HTTPException(status_code=400, detail="You cannot report yourself")
        if body.message_id is not None:
            msg_row = database.execute(
                "SELECT id FROM messages WHERE id = ? AND sender_id = ?",
                (body.message_id, target["id"]),
            ).fetchone()
            if msg_row is None:
                raise HTTPException(status_code=404, detail="Message not found for that user")
        database.execute(
            """
            INSERT INTO reports(reporter_user_id, reported_user_id, message_id, reason, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user["id"], target["id"], body.message_id, body.reason, body.details, now_iso()),
        )
    return {"reported": target["username"], "reason": body.reason}


@app.post("/api/rooms", status_code=201)
def create_room(body: CreateRoomBody, user: User) -> dict:
    expires_at = None
    if body.expires_in_hours:
        expires_at = (datetime.now(UTC) + timedelta(hours=body.expires_in_hours)).isoformat()
    lifetime_seconds = body.default_message_lifetime_hours * 3600
    room_code = generate_room_code()
    with connect() as database:
        for _ in range(5):
            try:
                cursor = database.execute(
                    """
                    INSERT INTO rooms(room_code, display_name, created_by_user_id, created_at,
                                     expires_at, default_message_lifetime_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (room_code, body.display_name, user["id"], now_iso(), expires_at, lifetime_seconds),
                )
                room_id = cursor.lastrowid or 0
                break
            except sqlite3.IntegrityError:
                room_code = generate_room_code()
        else:
            raise HTTPException(status_code=500, detail="Could not generate a unique room code")
        alias = assign_room_alias(database, room_id)
        database.execute(
            "INSERT INTO room_members(room_id, user_id, joined_at, alias) VALUES (?, ?, ?, ?)",
            (room_id, user["id"], now_iso(), alias),
        )
    return {
        "room_code": room_code,
        "display_name": body.display_name,
        "expires_at": expires_at,
        "default_message_lifetime_hours": body.default_message_lifetime_hours,
        "your_alias": alias,
    }


@app.get("/api/rooms")
def list_rooms(user: User) -> list[dict]:
    with connect() as database:
        rows = database.execute(
            """
            SELECT rooms.id, rooms.room_code, rooms.display_name, rooms.expires_at,
                   rooms.default_message_lifetime_seconds, room_members.alias
            FROM room_members
            JOIN rooms ON rooms.id = room_members.room_id
            WHERE room_members.user_id = ?
            ORDER BY rooms.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/rooms/join", status_code=201)
def join_room(body: JoinRoomBody, user: User) -> dict:
    with connect() as database:
        room = get_room(database, body.room_code)
        if room["expires_at"] and room["expires_at"] <= now_iso():
            raise HTTPException(status_code=410, detail="This room has expired")
        existing = database.execute(
            "SELECT alias FROM room_members WHERE room_id = ? AND user_id = ?",
            (room["id"], user["id"]),
        ).fetchone()
        if existing:
            return {
                "room_code": room["room_code"],
                "display_name": room["display_name"],
                "your_alias": existing["alias"],
                "already_member": True,
            }
        alias = assign_room_alias(database, room["id"])
        database.execute(
            "INSERT INTO room_members(room_id, user_id, joined_at, alias) VALUES (?, ?, ?, ?)",
            (room["id"], user["id"], now_iso(), alias),
        )
    return {
        "room_code": room["room_code"],
        "display_name": room["display_name"],
        "your_alias": alias,
        "already_member": False,
    }


@app.get("/api/rooms/{room_code}/messages")
def get_room_messages(room_code: str, user: User) -> list[dict]:
    purge_expired_messages()
    with connect() as database:
        room = get_room(database, room_code)
        membership = database.execute(
            "SELECT alias FROM room_members WHERE room_id = ? AND user_id = ?",
            (room["id"], user["id"]),
        ).fetchone()
        if membership is None:
            raise HTTPException(status_code=403, detail="You are not a member of this room")
        messages = database.execute(
            """
            SELECT rm.id, rm.body, rm.created_at, rm.expires_at,
                   rmb.alias AS sender_alias, rm.sender_id
            FROM room_messages rm
            JOIN room_members rmb ON rmb.room_id = rm.room_id AND rmb.user_id = rm.sender_id
            WHERE rm.room_id = ?
            ORDER BY rm.created_at
            """,
            (room["id"],),
        ).fetchall()
    return [
        {
            "id": msg["id"],
            "body": msg["body"],
            "created_at": msg["created_at"],
            "expires_at": msg["expires_at"],
            "sender_alias": msg["sender_alias"],
            "is_mine": msg["sender_id"] == user["id"],
        }
        for msg in messages
    ]


@app.post("/api/rooms/{room_code}/messages", status_code=201)
async def send_room_message(room_code: str, body: MessageBody, user: User) -> dict:
    with connect() as database:
        room = get_room(database, room_code)
        if room["expires_at"] and room["expires_at"] <= now_iso():
            raise HTTPException(status_code=410, detail="This room has expired")
        membership = database.execute(
            "SELECT alias FROM room_members WHERE room_id = ? AND user_id = ?",
            (room["id"], user["id"]),
        ).fetchone()
        if membership is None:
            raise HTTPException(status_code=403, detail="You are not a member of this room")
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=room["default_message_lifetime_seconds"])
        cursor = database.execute(
            """
            INSERT INTO room_messages(room_id, sender_id, body, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room["id"], user["id"], body.body, created_at.isoformat(), expires_at.isoformat()),
        )
        message_id = cursor.lastrowid
        sender_alias = membership["alias"]
        member_ids = [
            row["user_id"]
            for row in database.execute(
                "SELECT user_id FROM room_members WHERE room_id = ?", (room["id"],)
            ).fetchall()
        ]
    ws_payload = {
        "type": "room_message",
        "message": {
            "id": message_id,
            "room_id": room["id"],
            "room_code": room_code.upper(),
            "sender_alias": sender_alias,
            "body": body.body,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "is_mine": False,
        },
    }
    for uid in member_ids:
        if uid != user["id"]:
            await manager.notify(uid, ws_payload)
    return {
        "id": message_id,
        "room_id": room["id"],
        "room_code": room_code.upper(),
        "sender_alias": sender_alias,
        "body": body.body,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "is_mine": True,
    }


@app.get("/api/admin/reports")
def admin_list_reports(_: Admin) -> list[dict]:
    with connect() as database:
        rows = database.execute(
            """
            SELECT r.id, r.reason, r.details, r.created_at, r.status, r.message_id,
                   reporter.username AS reporter_username,
                   reported.username AS reported_username
            FROM reports r
            JOIN users AS reporter ON reporter.id = r.reporter_user_id
            JOIN users AS reported ON reported.id = r.reported_user_id
            ORDER BY r.created_at DESC
            """,
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/reports/{report_id}/status")
def admin_update_report_status(report_id: int, body: ReportStatusBody, _: Admin) -> dict:
    with connect() as database:
        row = database.execute("SELECT id FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Report not found")
        database.execute("UPDATE reports SET status = ? WHERE id = ?", (body.status, report_id))
    return {"id": report_id, "status": body.status}


@app.get("/api/admin/users")
def admin_list_users(_: Admin) -> list[dict]:
    with connect() as database:
        rows = database.execute(
            "SELECT id, username, created_at, disabled FROM users ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/users/{username}/disable")
def admin_disable_user(username: str, _: Admin) -> dict:
    with connect() as database:
        row = database.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        database.execute("UPDATE users SET disabled = 1 WHERE id = ?", (row["id"],))
    return {"username": username, "disabled": True}


@app.post("/api/admin/users/{username}/enable")
def admin_enable_user(username: str, _: Admin) -> dict:
    with connect() as database:
        row = database.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        database.execute("UPDATE users SET disabled = 0 WHERE id = ?", (row["id"],))
    return {"username": username, "disabled": False}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str) -> None:
    with connect() as database:
        user = database.execute("SELECT id FROM users WHERE token = ?", (token,)).fetchone()
    if user is None:
        await websocket.close(code=1008)
        return
    await manager.connect(user["id"], websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user["id"], websocket)


STATIC_PATH = ROOT / "static"
if STATIC_PATH.exists():
    app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_PATH / "index.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin.html", include_in_schema=False)
    def admin_page() -> FileResponse:
        return FileResponse(STATIC_PATH / "admin.html")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_PATH / "service-worker.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )
