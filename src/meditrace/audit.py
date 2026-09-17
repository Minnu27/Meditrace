"""Append-only audit logging. No route exposes update or delete for this table."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditEvent


def record(
    session: Session,
    *,
    user_email: str | None,
    role: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    patient_id: str | None = None,
    success: bool = True,
    detail: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            user_email=user_email,
            role=role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            patient_id=patient_id,
            success=success,
            detail=detail[:500] if detail else None,
        )
    )
    session.commit()
