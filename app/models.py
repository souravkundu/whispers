"""
SQLAlchemy ORM models.  Exact table names and column names match the existing
SQLite schema so that existing chat.db data is readable without a rename migration.

Case-insensitive username uniqueness is enforced by a functional unique index
on lower(username), which works in both SQLite ≥ 3.9 and PostgreSQL.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.db import Base, ISODateTime


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(24), nullable=False)
    token = Column(String(64), nullable=False, unique=True)
    password_hash = Column(Text, nullable=True)
    password_salt = Column(String(32), nullable=True)
    disabled = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        # Case-insensitive uniqueness via functional index (SQLite + PostgreSQL)
        Index("uq_users_username_lower", func.lower(username), unique=True),
    )


class Contact(Base):
    __tablename__ = "contacts"

    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    contact_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("owner_id != contact_id", name="ck_contacts_no_self"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    first_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    second_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expiry_hours = Column(Integer, nullable=False, default=24, server_default="24")
    created_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("first_user_id", "second_user_id", name="uq_conversations_pair"),
        CheckConstraint("first_user_id < second_user_id", name="ck_conversations_order"),
        Index("ix_conversations_participants", "first_user_id", "second_user_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(ISODateTime, nullable=False)
    expires_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        Index("messages_expiry_idx", "expires_at"),
    )


class BlockedUser(Base):
    __tablename__ = "blocked_users"

    blocker_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    blocked_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("blocker_user_id != blocked_user_id", name="ck_blocked_no_self"),
        Index("ix_blocked_lookup", "blocker_user_id", "blocked_user_id"),
    )


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reporter_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reported_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    reason = Column(String(32), nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(ISODateTime, nullable=False)
    status = Column(String(16), nullable=False, default="open", server_default="open")

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','reviewed','dismissed')",
            name="ck_reports_status",
        ),
        CheckConstraint(
            "reason IN ('spam','harassment','abuse','impersonation','other')",
            name="ck_reports_reason",
        ),
        Index("ix_reports_status_created", "status", "created_at"),
    )


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_code = Column(String(20), nullable=False, unique=True)
    display_name = Column(String(60), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(ISODateTime, nullable=False)
    expires_at = Column(ISODateTime, nullable=True)
    default_message_lifetime_seconds = Column(
        Integer, nullable=False, default=86400, server_default="86400"
    )

    __table_args__ = (
        CheckConstraint(
            "default_message_lifetime_seconds > 0",
            name="ck_rooms_lifetime_positive",
        ),
        Index("ix_rooms_code", "room_code"),
        Index("ix_rooms_expiry", "expires_at"),
    )


class RoomMember(Base):
    __tablename__ = "room_members"

    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    joined_at = Column(ISODateTime, nullable=False)
    alias = Column(String(60), nullable=False)

    __table_args__ = (
        Index("ix_room_members_lookup", "room_id", "user_id"),
    )


class RoomMessage(Base):
    __tablename__ = "room_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(ISODateTime, nullable=False)
    expires_at = Column(ISODateTime, nullable=False)

    __table_args__ = (
        Index("room_messages_expiry_idx", "expires_at"),
        Index("ix_room_messages_room_created", "room_id", "created_at"),
    )
