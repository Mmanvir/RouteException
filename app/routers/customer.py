"""Customer API: only approved+sent updates, never driver details or GPS. Fraud report -> evidence pack."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_rules, get_settings
from app.database import get_db
from app.models import CustomerMessage, FraudReport, Shipment
from app.schemas import FraudReportIn
from app.services.audit import log_event
from app.services.evidence_pack import build_evidence_pack, is_complete
from app.services.extraction import PHOTO_NAMES

router = APIRouter(prefix="/api/customer", tags=["customer"])


class CustomerUpdate(BaseModel):
    id: int
    text: str
    sent_at: str
    audio_url: str | None


class CustomerView(BaseModel):
    tracking_id: str
    updates: list[CustomerUpdate]


class FraudOut(BaseModel):
    report_id: int
    evidence_pack_url: str
    helpline: str
    portal: str
    note: str


def _by_tracking(db: Session, tid: str) -> Shipment:
    sh = db.scalar(select(Shipment).where(Shipment.tracking_id == tid))
    if sh is None:
        raise HTTPException(404, "tracking id not found")
    return sh


def _sent(sh: Shipment) -> list[CustomerMessage]:
    return [m for m in sh.messages if m.status == "sent" and m.final_text]


@router.get("/{tid}", response_model=CustomerView)
def view(tid: str, db: Session = Depends(get_db)) -> CustomerView:
    sh = _by_tracking(db, tid)
    return CustomerView(tracking_id=tid, updates=[
        CustomerUpdate(
            id=m.id, text=m.final_text or "", sent_at=f"{m.sent_at:%d %b %Y, %H:%M} UTC",
            audio_url=f"/api/customer/{tid}/audio/{m.id}" if m.audio_path else None,
        ) for m in reversed(_sent(sh))
    ])


@router.get("/{tid}/audio/{mid}")
def audio(tid: str, mid: int, db: Session = Depends(get_db)) -> FileResponse:
    sh = _by_tracking(db, tid)
    m = next((m for m in _sent(sh) if m.id == mid and m.audio_path), None)
    if m is None:
        raise HTTPException(404)
    return FileResponse(m.audio_path, media_type="audio/mpeg")


@router.post("/{tid}/fraud-report", response_model=FraudOut)
def fraud_report(tid: str, body: FraudReportIn, db: Session = Depends(get_db)) -> FraudOut:
    """Builds an evidence pack for the customer. Nobody is contacted or accused automatically."""
    sh = _by_tracking(db, tid)
    fraud_cfg = get_rules()["fraud"]
    tracking = (sh.analysis or {}).get("extraction", {}).get("tracking", [])
    timeline = [{"time": r["timestamp"].replace("T", " ")[:16], "status": r["status"].replace("_", " "),
                 "location": r.get("location", "")} for r in tracking]  # no lat/lon on purpose
    case = Path(sh.case_dir)
    photo = next((case / n for n in PHOTO_NAMES if (case / n).exists()), None)

    report = FraudReport(shipment_id=sh.id, description=body.description, pdf_path="")
    db.add(report)
    db.flush()
    pdf = get_settings().storage_dir / "evidence_packs" / f"{sh.id}_report{report.id}.pdf"
    sections = build_evidence_pack(
        pdf,
        shipment={"id": sh.id, "tracking_id": sh.tracking_id, "address": sh.address},
        timeline=timeline,
        photo_path=photo,
        sent_messages=[{"sent_at": f"{m.sent_at:%d %b %Y %H:%M}", "text": m.final_text} for m in _sent(sh)],
        report_text=body.description,
        fraud_cfg=fraud_cfg,
    )
    report.pdf_path = str(pdf)
    log_event(db, sh.id, "customer", "fraud_report", {"report_id": report.id, "sections": sections,
                                                       "complete": is_complete(sections)})
    db.commit()
    return FraudOut(
        report_id=report.id,
        evidence_pack_url=f"/api/customer/{tid}/evidence-pack/{report.id}",
        helpline=fraud_cfg["helpline"],
        portal=fraud_cfg["portal"],
        note="We have not contacted anyone on your behalf. You can file the complaint using this pack.",
    )


@router.get("/{tid}/evidence-pack/{rid}")
def evidence_pack(tid: str, rid: int, db: Session = Depends(get_db)) -> FileResponse:
    sh = _by_tracking(db, tid)
    r = db.get(FraudReport, rid)
    if r is None or r.shipment_id != sh.id:
        raise HTTPException(404)
    return FileResponse(r.pdf_path, media_type="application/pdf", filename=f"evidence_pack_{tid}.pdf")
