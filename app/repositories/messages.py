from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Conversation, Message, User


# ── Conversations ─────────────────────────────────────────────────────────────

def get_conversation(db: Session, user_id: int, conversation_id: int) -> Conversation | None:
    return (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            (Conversation.first_user_id == user_id) | (Conversation.second_user_id == user_id),
        )
        .first()
    )


def open_or_get_conversation(
    db: Session,
    *,
    first_user_id: int,
    second_user_id: int,
    expiry_hours: int,
    created_at: datetime,
) -> Conversation:
    existing = (
        db.query(Conversation)
        .filter(
            Conversation.first_user_id == first_user_id,
            Conversation.second_user_id == second_user_id,
        )
        .first()
    )
    if existing:
        return existing
    convo = Conversation(
        first_user_id=first_user_id,
        second_user_id=second_user_id,
        expiry_hours=expiry_hours,
        created_at=created_at,
    )
    db.add(convo)
    db.flush()
    return convo


def update_expiry(db: Session, conversation_id: int, expiry_hours: int) -> None:
    db.query(Conversation).filter(Conversation.id == conversation_id).update(
        {"expiry_hours": expiry_hours}
    )


# ── Messages ──────────────────────────────────────────────────────────────────

def list_messages(db: Session, conversation_id: int) -> list[dict]:
    rows = (
        db.query(
            Message.id,
            Message.body,
            Message.created_at,
            Message.expires_at,
            User.username.label("sender"),
        )
        .join(User, User.id == Message.sender_id)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "body": r.body,
            "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else r.created_at,
            "expires_at": r.expires_at.isoformat() if isinstance(r.expires_at, datetime) else r.expires_at,
            "sender": r.sender,
        }
        for r in rows
    ]


def create_message(
    db: Session,
    *,
    conversation_id: int,
    sender_id: int,
    body: str,
    created_at: datetime,
    expires_at: datetime,
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        body=body,
        created_at=created_at,
        expires_at=expires_at,
    )
    db.add(msg)
    db.flush()
    return msg


def get_message_by_id_and_sender(db: Session, message_id: int, sender_id: int) -> Message | None:
    return (
        db.query(Message)
        .filter(Message.id == message_id, Message.sender_id == sender_id)
        .first()
    )


def purge_expired(db: Session, now: datetime) -> int:
    count = db.query(Message).filter(Message.expires_at <= now).delete(synchronize_session=False)
    return count
