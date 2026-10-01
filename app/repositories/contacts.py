from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Contact, User


def list_for_user(db: Session, owner_id: int) -> list[dict]:
    rows = (
        db.query(User.id, User.username)
        .join(Contact, Contact.contact_id == User.id)
        .filter(Contact.owner_id == owner_id)
        .order_by(func.lower(User.username))
        .all()
    )
    return [{"id": r.id, "username": r.username} for r in rows]


def add(db: Session, owner_id: int, contact_id: int, created_at) -> None:
    existing = (
        db.query(Contact)
        .filter(Contact.owner_id == owner_id, Contact.contact_id == contact_id)
        .first()
    )
    if existing is None:
        db.add(Contact(owner_id=owner_id, contact_id=contact_id, created_at=created_at))
        db.flush()


def exists(db: Session, owner_id: int, contact_id: int) -> bool:
    return (
        db.query(Contact)
        .filter(Contact.owner_id == owner_id, Contact.contact_id == contact_id)
        .first()
        is not None
    )
