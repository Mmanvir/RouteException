"""Dispatcher API: intake, review, edit, approve, send. Thin: validation + service calls only."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_rules, get_settings
from app.database import get_db
from app.models import AuditLog, CustomerMessage, Shipment
from app.schemas import (
    CAUSES, Analysis, ApproveIn, AuditOut, CauseResult, CheckTextIn, EditDraftIn, GuardrailResult,
    MessageOut, ShipmentDetail, ShipmentSummary,
)
from app.services.audit import log_event
from app.services.guardrail import check_text
from app.services.messages import build_task_card, draft_customer_message, private_terms, unsupported_facts
from app.services.next_action import decide_next_action
from app.services.pipeline import process_shipment
from app.services.tts import synthesize

router = APIRouter(prefix="/shipments", tags=["dispatcher"])
SERVABLE = {"pod.jpg", "pod.jpeg", "pod.png", "driver_note.mp3", "driver_note.wav", "driver_note.m4a",
            "driver_note.webm", "driver_note.ogg"}


# ------------------------------------------------------------------ helpers
def _case_root() -> Path:
    p = get_settings().storage_dir / "cases"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get(db: Session, sid: str) -> Shipment:
    sh = db.get(Shipment, sid)
    if sh is None:
        raise HTTPException(404, f"shipment {sid} not found")
    return sh


def _draft(sh: Shipment) -> CustomerMessage | None:
    drafts = [m for m in sh.messages if m.status == "draft"]
    return drafts[-1] if drafts else None


def _summary(sh: Shipment) -> ShipmentSummary:
    return ShipmentSummary(
        id=sh.id, tracking_id=sh.tracking_id, status=sh.status, address=sh.address, cause=sh.cause,
        confidence=sh.confidence, contradiction_count=sh.contradiction_count, updated_at=sh.updated_at,
    )


def _msg_out(m: CustomerMessage) -> MessageOut:
    return MessageOut(
        id=m.id, draft_text=m.draft_text, final_text=m.final_text, status=m.status,
        has_audio=bool(m.audio_path), approved_by=m.approved_by, approved_at=m.approved_at, sent_at=m.sent_at,
    )


def _detail(sh: Shipment) -> ShipmentDetail:
    return ShipmentDetail(**_summary(sh).model_dump(), analysis=sh.analysis, error=sh.error,
                          messages=[_msg_out(m) for m in sh.messages])


def register_case_dir(db: Session, src: Path, bg: BackgroundTasks, actor: str = "dispatcher") -> Shipment:
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    sid = meta["shipment_id"]
    dest = _case_root() / sid
    if dest.resolve() != src.resolve():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    sh = db.get(Shipment, sid)
    if sh is None:
        sh = Shipment(id=sid, tracking_id=meta["tracking_id"], case_dir=str(dest))
        db.add(sh)
    sh.address = meta.get("address", "")
    sh.status = "processing"
    log_event(db, sid, actor, "shipment_received", {"source": src.name})
    db.commit()
    bg.add_task(process_shipment, sid)
    return sh


def _guard(sh: Shipment, text: str) -> tuple[GuardrailResult, list[str]]:
    a = Analysis.model_validate(sh.analysis) if sh.analysis else None
    terms = private_terms(a.extraction.meta) if a else []
    return check_text(text, terms), (unsupported_facts(text, a.extraction) if a else [])


# ------------------------------------------------------------------ intake
@router.post("", status_code=202, response_model=ShipmentSummary)
async def upload_shipment(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    meta_json: UploadFile = File(..., description="meta.json: shipment_id, tracking_id, address, ..."),
    tracking_csv: UploadFile = File(...),
    photo: UploadFile | None = File(None),
    driver_note: UploadFile | None = File(None),
    driver_note_text: str | None = Form(None),
    customer_txt: UploadFile | None = File(None),
) -> ShipmentSummary:
    """Intake: save files, create shipment with status 'processing', return immediately."""
    raw = await meta_json.read()
    try:
        meta = json.loads(raw)
        sid, _ = meta["shipment_id"], meta["tracking_id"]
    except (json.JSONDecodeError, KeyError) as e:
        raise HTTPException(422, f"meta.json invalid: {e}") from e
    if not str(sid).replace("-", "").isalnum():
        raise HTTPException(422, "shipment_id must be alphanumeric")
    staging = _case_root() / sid
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "meta.json").write_bytes(raw)
    (staging / "tracking.csv").write_bytes(await tracking_csv.read())
    if photo and photo.filename:
        ext = Path(photo.filename).suffix.lower() if Path(photo.filename).suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
        (staging / f"pod{ext}").write_bytes(await photo.read())
    if driver_note and driver_note.filename:
        ext = Path(driver_note.filename).suffix.lower() or ".mp3"
        (staging / f"driver_note{ext}").write_bytes(await driver_note.read())
    if driver_note_text:
        (staging / "driver_note.txt").write_text(driver_note_text, encoding="utf-8")
    if customer_txt and customer_txt.filename:
        (staging / "customer.txt").write_bytes(await customer_txt.read())
    return _summary(register_case_dir(db, staging, bg))


class ImportResult(BaseModel):
    imported: list[str]


@router.post("/import-demo", response_model=ImportResult)
def import_demo(bg: BackgroundTasks, db: Session = Depends(get_db)) -> ImportResult:
    """Load every folder in data/cases (synthetic demo data)."""
    src_root = get_settings().cases_dir
    dirs = sorted(d for d in src_root.iterdir() if (d / "meta.json").exists()) if src_root.exists() else []
    if not dirs:
        raise HTTPException(404, "no demo cases; run: python scripts/generate_data.py")
    return ImportResult(imported=[register_case_dir(db, d, bg).id for d in dirs])


# ------------------------------------------------------------------ read
@router.get("", response_model=list[ShipmentSummary])
def list_shipments(status: str | None = None, db: Session = Depends(get_db)) -> list[ShipmentSummary]:
    q = select(Shipment).order_by(Shipment.contradiction_count.desc(), Shipment.id)
    if status:
        q = q.where(Shipment.status == status)
    return [_summary(s) for s in db.scalars(q)]


@router.get("/{sid}", response_model=ShipmentDetail)
def get_shipment(sid: str, db: Session = Depends(get_db)) -> ShipmentDetail:
    return _detail(_get(db, sid))


@router.get("/{sid}/files/{name}")
def get_file(sid: str, name: str, db: Session = Depends(get_db)) -> FileResponse:
    if name not in SERVABLE:
        raise HTTPException(404)
    path = Path(_get(db, sid).case_dir) / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@router.get("/{sid}/audio/{mid}")
def get_audio(sid: str, mid: int, db: Session = Depends(get_db)) -> FileResponse:
    m = db.get(CustomerMessage, mid)
    if not m or m.shipment_id != sid or not m.audio_path:
        raise HTTPException(404)
    return FileResponse(m.audio_path, media_type="audio/mpeg")


@router.get("/{sid}/audit", response_model=list[AuditOut])
def shipment_audit(sid: str, db: Session = Depends(get_db)) -> list[AuditOut]:
    rows = db.scalars(select(AuditLog).where(AuditLog.shipment_id == sid).order_by(AuditLog.id))
    return [AuditOut.model_validate(r, from_attributes=True) for r in rows]


# ------------------------------------------------------------------ review actions
@router.post("/{sid}/reprocess", response_model=ShipmentSummary, status_code=202)
def reprocess(sid: str, bg: BackgroundTasks, db: Session = Depends(get_db)) -> ShipmentSummary:
    sh = _get(db, sid)
    sh.status = "processing"
    log_event(db, sid, "dispatcher", "reprocess_requested")
    db.commit()
    bg.add_task(process_shipment, sid)
    return _summary(sh)


@router.post("/check-text", response_model=GuardrailResult)
def check(body: CheckTextIn) -> GuardrailResult:
    return check_text(body.text)


class SetCauseIn(BaseModel):
    cause: str
    actor: str = "dispatcher"
    note: str = ""


@router.post("/{sid}/cause", response_model=ShipmentDetail)
def set_cause(sid: str, body: SetCauseIn, db: Session = Depends(get_db)) -> ShipmentDetail:
    """Dispatcher confirms/overrides the cause. Next action is re-looked-up from rules.yaml."""
    if body.cause not in CAUSES:
        raise HTTPException(422, f"cause must be one of {CAUSES}")
    sh = _get(db, sid)
    if not sh.analysis or sh.status not in {"needs_review"}:
        raise HTTPException(409, "cause can only be changed while the shipment is in needs_review")
    a = Analysis.model_validate(sh.analysis)
    prev = a.cause.cause
    a.cause = CauseResult(
        cause=body.cause, candidate=body.cause, confidence=1.0, needs_review=False,  # type: ignore[arg-type]
        reasoning=f"Set by {body.actor}. {body.note}".strip(), evidence_ids=a.cause.evidence_ids,
        engine="dispatcher",
    )
    rules_cfg = get_rules()
    a.next_action = decide_next_action(a.cause, a.contradictions, rules_cfg)
    a.task_card = build_task_card(a.extraction, a.timeline, a.contradictions, a.cause, a.next_action)
    a.customer_draft, a.draft_engine, a.guardrail, a.unsupported_facts = draft_customer_message(
        a.extraction, a.cause, a.next_action)
    sh.analysis = a.model_dump(mode="json")
    sh.cause, sh.confidence = a.cause.cause, 1.0
    d = _draft(sh)
    if d:
        d.draft_text, d.final_text = a.customer_draft, None
    log_event(db, sid, body.actor, "cause_set", {"from": prev, "to": body.cause, "note": body.note})
    db.commit()
    return _detail(sh)


class EditOut(BaseModel):
    message: MessageOut
    guardrail: GuardrailResult
    unsupported_facts: list[str]


@router.put("/{sid}/message", response_model=EditOut)
def edit_draft(sid: str, body: EditDraftIn, db: Session = Depends(get_db)) -> EditOut:
    sh = _get(db, sid)
    d = _draft(sh)
    if d is None:
        raise HTTPException(409, "no draft to edit")
    before = d.final_text or d.draft_text
    d.final_text = body.text
    g, bad = _guard(sh, body.text)
    log_event(db, sid, body.editor, "draft_edited", {"before": before, "after": body.text, "guardrail_ok": g.ok})
    db.commit()
    return EditOut(message=_msg_out(d), guardrail=g, unsupported_facts=bad)


@router.post("/{sid}/approve", response_model=ShipmentDetail)
def approve(sid: str, body: ApproveIn, db: Session = Depends(get_db)) -> ShipmentDetail:
    """Human approval. Guardrail must pass. Only now is audio generated."""
    sh = _get(db, sid)
    if sh.status != "needs_review":
        raise HTTPException(409, f"shipment is '{sh.status}', expected 'needs_review'")
    if sh.cause == "needs_review":
        raise HTTPException(409, "confirm a cause before approving (POST /shipments/{id}/cause)")
    d = _draft(sh)
    if d is None:
        raise HTTPException(409, "no draft to approve")
    text = (body.text or d.final_text or d.draft_text).strip()
    g, bad = _guard(sh, text)
    if not g.ok or bad:
        log_event(db, sid, body.approver, "approval_blocked",
                  {"violations": g.violations, "unsupported_facts": bad})
        db.commit()
        raise HTTPException(422, {"message": "guardrail blocked this text", "violations": g.violations,
                                  "unsupported_facts": bad})
    if text != (d.final_text or d.draft_text):
        log_event(db, sid, body.approver, "draft_edited", {"before": d.final_text or d.draft_text, "after": text})
    d.final_text = text
    d.status = "approved"
    d.approved_by = body.approver
    d.approved_at = datetime.now(timezone.utc)
    audio = synthesize(text, get_settings().storage_dir / "audio" / f"{sid}_{d.id}.mp3")
    d.audio_path = str(audio) if audio else None
    sh.status = "approved"
    log_event(db, sid, body.approver, "message_approved",
              {"message_id": d.id, "text": text, "audio": bool(audio), "cause": sh.cause})
    db.commit()
    return _detail(sh)


@router.post("/{sid}/send", response_model=ShipmentDetail)
def send(sid: str, actor: str = "dispatcher", db: Session = Depends(get_db)) -> ShipmentDetail:
    """Release the approved update to the customer tracking page."""
    sh = _get(db, sid)
    approved = [m for m in sh.messages if m.status == "approved"]
    if sh.status != "approved" or not approved:
        raise HTTPException(409, "nothing approved to send")
    for m in approved:
        m.status = "sent"
        m.sent_at = datetime.now(timezone.utc)
    sh.status = "sent"
    log_event(db, sid, actor, "message_sent", {"message_ids": [m.id for m in approved]})
    db.commit()
    return _detail(sh)


audit_router = APIRouter(tags=["dispatcher"])


@audit_router.get("/audit", response_model=list[AuditOut])
def all_audit(limit: int = 200, db: Session = Depends(get_db)) -> list[AuditOut]:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    return [AuditOut.model_validate(r, from_attributes=True) for r in rows]
