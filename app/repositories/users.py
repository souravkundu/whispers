from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import User


def get_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_by_token(db: Session, token: str) -> User | None:
    return db.query(User).filter(User.token == token).first()


def get_by_username(db: Session, username: str) -> User | None:
    return (
        db.query(User)
        .filter(func.lower(User.username) == username.lower())
        .first()
    )


def create(db: Session, *, username: str, token: str, created_at) -> User:
    user = User(username=username, token=token, created_at=created_at)
    db.add(user)
    db.flush()  # get id without committing
    return user


def update_token(db: Session, user_id: int, token: str) -> None:
    db.query(User).filter(User.id == user_id).update({"token": token})


def set_password(db: Session, user_id: int, password_hash: str, password_salt: str) -> None:
    db.query(User).filter(User.id == user_id).update(
        {"password_hash": password_hash, "password_salt": password_salt}
    )


def has_password(db: Session, user_id: int) -> bool:
    row = db.query(User.password_hash).filter(User.id == user_id).first()
    return bool(row and row.password_hash)


def disable(db: Session, username: str) -> User | None:
    user = get_by_username(db, username)
    if user is not None:
        user.disabled = True
        db.flush()
    return user


def enable(db: Session, username: str) -> User | None:
    user = get_by_username(db, username)
    if user is not None:
        user.disabled = False
        db.flush()
    return user


def list_all(db: Session) -> list[User]:
    return db.query(User).order_by(User.created_at.desc()).all()
