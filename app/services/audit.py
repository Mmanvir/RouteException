"""Audit log: every approval, edit and fraud report is recorded."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def log_event(db: Session, shipment_id: str | None, actor: str, action: str,
              details: dict[str, Any] | None = None) -> None:
    db.add(AuditLog(shipment_id=shipment_id, actor=actor, action=action, details=details or {}))
