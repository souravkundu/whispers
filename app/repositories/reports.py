from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Report, User


def create(
    db: Session,
    *,
    reporter_id: int,
    reported_id: int,
    message_id: int | None,
    reason: str,
    details: str | None,
    created_at: datetime,
) -> Report:
    report = Report(
        reporter_user_id=reporter_id,
        reported_user_id=reported_id,
        message_id=message_id,
        reason=reason,
        details=details,
        created_at=created_at,
    )
    db.add(report)
    db.flush()
    return report


def list_all(db: Session) -> list[dict]:
    reporter = User.__table__.alias("reporter")
    reported = User.__table__.alias("reported")
    rows = (
        db.query(
            Report.id,
            Report.reason,
            Report.details,
            Report.created_at,
            Report.status,
            Report.message_id,
            reporter.c.username.label("reporter_username"),
            reported.c.username.label("reported_username"),
        )
        .join(reporter, reporter.c.id == Report.reporter_user_id)
        .join(reported, reported.c.id == Report.reported_user_id)
        .order_by(Report.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "reason": r.reason,
            "details": r.details,
            "created_at": r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else r.created_at,
            "status": r.status,
            "message_id": r.message_id,
            "reporter_username": r.reporter_username,
            "reported_username": r.reported_username,
        }
        for r in rows
    ]


def get_by_id(db: Session, report_id: int) -> Report | None:
    return db.get(Report, report_id)


def update_status(db: Session, report_id: int, status: str) -> None:
    db.query(Report).filter(Report.id == report_id).update({"status": status})
