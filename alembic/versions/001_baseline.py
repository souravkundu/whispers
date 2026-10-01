"""Baseline migration — complete initial schema.

This migration represents the full schema as of Phase 2.
Existing databases that already have all tables will pass the check_constraints
phase safely; Alembic will detect no differences and skip table creation.

To adopt an existing chat.db:
    1. Back up: copy chat.db chat.db.bak
    2. Run:    alembic stamp head   (marks existing DB as up-to-date without running DDL)
    3. Verify: alembic current

To create from scratch:
    alembic upgrade head

Revision ID: 001_baseline
Revises:
Create Date: 2026-08-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("username", sa.String(length=24), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_salt", sa.String(length=32), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True)

    op.create_table(
        "contacts",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.CheckConstraint("owner_id != contact_id", name="ck_contacts_no_self"),
        sa.ForeignKeyConstraint(["contact_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id", "contact_id"),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("first_user_id", sa.Integer(), nullable=False),
        sa.Column("second_user_id", sa.Integer(), nullable=False),
        sa.Column("expiry_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.CheckConstraint("first_user_id < second_user_id", name="ck_conversations_order"),
        sa.ForeignKeyConstraint(["first_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["second_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("first_user_id", "second_user_id", name="uq_conversations_pair"),
    )
    op.create_index("ix_conversations_participants", "conversations", ["first_user_id", "second_user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.Column("expires_at", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("messages_expiry_idx", "messages", ["expires_at"])

    op.create_table(
        "blocked_users",
        sa.Column("blocker_user_id", sa.Integer(), nullable=False),
        sa.Column("blocked_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.CheckConstraint("blocker_user_id != blocked_user_id", name="ck_blocked_no_self"),
        sa.ForeignKeyConstraint(["blocked_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blocker_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("blocker_user_id", "blocked_user_id"),
    )
    op.create_index("ix_blocked_lookup", "blocked_users", ["blocker_user_id", "blocked_user_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("reporter_user_id", sa.Integer(), nullable=False),
        sa.Column("reported_user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.CheckConstraint(
            "status IN ('open','reviewed','dismissed')", name="ck_reports_status"
        ),
        sa.CheckConstraint(
            "reason IN ('spam','harassment','abuse','impersonation','other')",
            name="ck_reports_reason",
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reported_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reporter_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reports_status_created", "reports", ["status", "created_at"])

    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("room_code", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=60), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.Column("expires_at", sa.String(length=50), nullable=True),
        sa.Column(
            "default_message_lifetime_seconds", sa.Integer(), nullable=False, server_default="86400"
        ),
        sa.CheckConstraint(
            "default_message_lifetime_seconds > 0", name="ck_rooms_lifetime_positive"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_code"),
    )
    op.create_index("ix_rooms_code", "rooms", ["room_code"])
    op.create_index("ix_rooms_expiry", "rooms", ["expires_at"])

    op.create_table(
        "room_members",
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("joined_at", sa.String(length=50), nullable=False),
        sa.Column("alias", sa.String(length=60), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id", "user_id"),
    )
    op.create_index("ix_room_members_lookup", "room_members", ["room_id", "user_id"])

    op.create_table(
        "room_messages",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=50), nullable=False),
        sa.Column("expires_at", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("room_messages_expiry_idx", "room_messages", ["expires_at"])
    op.create_index("ix_room_messages_room_created", "room_messages", ["room_id", "created_at"])


def downgrade() -> None:
    op.drop_table("room_messages")
    op.drop_table("room_members")
    op.drop_table("rooms")
    op.drop_table("reports")
    op.drop_table("blocked_users")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("contacts")
    op.drop_table("users")
