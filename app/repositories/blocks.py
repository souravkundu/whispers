from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import BlockedUser, User


def is_blocked(db: Session, blocker_id: int, blocked_id: int) -> bool:
    return (
        db.query(BlockedUser)
        .filter(
            BlockedUser.blocker_user_id == blocker_id,
            BlockedUser.blocked_user_id == blocked_id,
        )
        .first()
        is not None
    )


def block(db: Session, blocker_id: int, blocked_id: int, created_at: datetime) -> None:
    if not is_blocked(db, blocker_id, blocked_id):
        db.add(BlockedUser(blocker_user_id=blocker_id, blocked_user_id=blocked_id, created_at=created_at))
        db.flush()


def unblock(db: Session, blocker_id: int, blocked_id: int) -> None:
    db.query(BlockedUser).filter(
        BlockedUser.blocker_user_id == blocker_id,
        BlockedUser.blocked_user_id == blocked_id,
    ).delete(synchronize_session=False)


def list_blocked(db: Session, user_id: int) -> list[dict]:
    rows = (
        db.query(User.id, User.username)
        .join(BlockedUser, BlockedUser.blocked_user_id == User.id)
        .filter(BlockedUser.blocker_user_id == user_id)
        .order_by(func.lower(User.username))
        .all()
    )
    return [{"id": r.id, "username": r.username} for r in rows]
