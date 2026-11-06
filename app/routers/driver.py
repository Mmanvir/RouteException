"""Driver API: live delivery updates from the mobile web app (photo, GPS, status, voice note)."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Shipment
from app.services.audit import log_event
from app.services.extraction import AUDIO_NAMES, PHOTO_NAMES
from app.services.pipeline import process_shipment

router = APIRouter(prefix="/api/driver", tags=["driver"])
DRIVER_STATUSES = {"out_for_delivery", "delivered", "attempted", "damaged_reported", "returned_to_hub"}


class DriverStop(BaseModel):
    id: str
    tracking_id: str
    address: str
    customer_first_name: str
    gate_number: str
    status: str


@router.get("/stops", response_model=list[DriverStop])
def stops(db: Session = Depends(get_db)) -> list[DriverStop]:
    out = []
    for sh in db.scalars(select(Shipment).order_by(Shipment.id)):
        meta = json.loads((Path(sh.case_dir) / "meta.json").read_text(encoding="utf-8"))
        out.append(DriverStop(
            id=sh.id, tracking_id=sh.tracking_id, address=sh.address,
            customer_first_name=meta.get("customer_first_name", ""),
            gate_number=str(meta.get("gate_number", "")), status=sh.status,
        ))
    return out


class UpdateOut(BaseModel):
    shipment_id: str
    status: str
    recorded: list[str]


@router.post("/stops/{sid}/update", response_model=UpdateOut, status_code=202)
async def update(
    sid: str,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    status: str = Form(...),
    lat: float | None = Form(None),
    lon: float | None = Form(None),
    location: str = Form(""),
    note_text: str | None = Form(None),
    photo: UploadFile | None = File(None),
    voice: UploadFile | None = File(None),
) -> UpdateOut:
    if status not in DRIVER_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(DRIVER_STATUSES)}")
    sh = db.get(Shipment, sid)
    if sh is None:
        raise HTTPException(404)
    case = Path(sh.case_dir)
    meta_path = case / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recorded = [f"status:{status}"]

    with open(case / "tracking.csv", "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow([now, status, location or "driver app", "" if lat is None else lat,
                                 "" if lon is None else lon])
    if lat is not None:
        recorded.append("gps")

    if photo and photo.filename:
        for n in PHOTO_NAMES:
            (case / n).unlink(missing_ok=True)
        ext = ".png" if photo.filename.lower().endswith(".png") else ".jpg"
        (case / f"pod{ext}").write_bytes(await photo.read())
        meta["photo_taken_at"] = now
        recorded.append("photo")

    if voice and voice.filename:
        for n in AUDIO_NAMES:
            (case / n).unlink(missing_ok=True)
        ext = Path(voice.filename).suffix.lower() or ".webm"
        (case / f"driver_note{ext}").write_bytes(await voice.read())
        meta["driver_note_recorded_at"] = now
        meta["transcript_is_cached"] = False
        recorded.append("voice_note")

    if note_text and note_text.strip():
        (case / "driver_note.txt").write_text(note_text.strip(), encoding="utf-8")
        meta.setdefault("driver_note_recorded_at", now)
        meta["driver_note_recorded_at"] = now
        meta["transcript_is_cached"] = False
        recorded.append("typed_note")

    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    sh.status = "processing"
    log_event(db, sid, "driver", "driver_update", {"recorded": recorded, "status": status})
    db.commit()
    bg.add_task(process_shipment, sid)
    return UpdateOut(shipment_id=sid, status="processing", recorded=recorded)
