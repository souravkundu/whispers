from __future__ import annotations

import random
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Room, RoomMember, RoomMessage, User

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


def assign_alias(db: Session, room_id: int) -> str:
    existing = {
        row[0]
        for row in db.query(RoomMember.alias).filter(RoomMember.room_id == room_id).all()
    }
    for _ in range(400):
        alias = f"{random.choice(_ALIAS_ADJ)}{random.choice(_ALIAS_NOUN)}"
        if alias not in existing:
            return alias
    return f"{random.choice(_ALIAS_ADJ)}{random.choice(_ALIAS_NOUN)}{random.randint(2, 99)}"


def create_room(
    db: Session,
    *,
    room_code: str,
    display_name: str,
    created_by_user_id: int,
    created_at: datetime,
    expires_at: datetime | None,
    default_message_lifetime_seconds: int,
) -> Room:
    room = Room(
        room_code=room_code,
        display_name=display_name,
        created_by_user_id=created_by_user_id,
        created_at=created_at,
        expires_at=expires_at,
        default_message_lifetime_seconds=default_message_lifetime_seconds,
    )
    db.add(room)
    db.flush()
    return room


def get_by_code(db: Session, room_code: str) -> Room | None:
    return db.query(Room).filter(Room.room_code == room_code.upper()).first()


def list_for_user(db: Session, user_id: int) -> list[dict]:
    rows = (
        db.query(
            Room.id,
            Room.room_code,
            Room.display_name,
            Room.expires_at,
            Room.default_message_lifetime_seconds,
            RoomMember.alias,
        )
        .join(RoomMember, RoomMember.room_id == Room.id)
        .filter(RoomMember.user_id == user_id)
        .order_by(Room.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "room_code": r.room_code,
            "display_name": r.display_name,
            "expires_at": r.expires_at.isoformat() if r.expires_at and hasattr(r.expires_at, "isoformat") else r.expires_at,
            "default_message_lifetime_seconds": r.default_message_lifetime_seconds,
            "alias": r.alias,
        }
        for r in rows
    ]


def get_membership(db: Session, room_id: int, user_id: int) -> RoomMember | None:
    return (
        db.query(RoomMember)
        .filter(RoomMember.room_id == room_id, RoomMember.user_id == user_id)
        .first()
    )


def add_member(db: Session, room_id: int, user_id: int, alias: str, joined_at: datetime) -> RoomMember:
    member = RoomMember(room_id=room_id, user_id=user_id, alias=alias, joined_at=joined_at)
    db.add(member)
    db.flush()
    return member


def list_member_ids(db: Session, room_id: int) -> list[int]:
    return [row[0] for row in db.query(RoomMember.user_id).filter(RoomMember.room_id == room_id).all()]


def create_message(
    db: Session,
    *,
    room_id: int,
    sender_id: int,
    body: str,
    created_at: datetime,
    expires_at: datetime,
) -> RoomMessage:
    msg = RoomMessage(
        room_id=room_id,
        sender_id=sender_id,
        body=body,
        created_at=created_at,
        expires_at=expires_at,
    )
    db.add(msg)
    db.flush()
    return msg


def list_messages(db: Session, room_id: int, requesting_user_id: int) -> list[dict]:
    rows = (
        db.query(
            RoomMessage.id,
            RoomMessage.body,
            RoomMessage.created_at,
            RoomMessage.expires_at,
            RoomMessage.sender_id,
            RoomMember.alias.label("sender_alias"),
        )
        .join(
            RoomMember,
            (RoomMember.room_id == RoomMessage.room_id)
            & (RoomMember.user_id == RoomMessage.sender_id),
        )
        .filter(RoomMessage.room_id == room_id)
        .order_by(RoomMessage.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "body": r.body,
            "created_at": r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else r.created_at,
            "expires_at": r.expires_at.isoformat() if hasattr(r.expires_at, "isoformat") else r.expires_at,
            "sender_alias": r.sender_alias,
            "is_mine": r.sender_id == requesting_user_id,
        }
        for r in rows
    ]


def purge_expired_messages(db: Session, now: datetime) -> int:
    return db.query(RoomMessage).filter(RoomMessage.expires_at <= now).delete(
        synchronize_session=False
    )


def purge_expired_rooms(db: Session, now: datetime) -> int:
    return (
        db.query(Room)
        .filter(Room.expires_at.isnot(None), Room.expires_at <= now)
        .delete(synchronize_session=False)
    )
