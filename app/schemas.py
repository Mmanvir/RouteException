"""Pydantic models shared by the pipeline and the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CAUSES = ("address_issue", "customer_unavailable", "damaged", "wrong_delivery", "delayed")
Cause = Literal["address_issue", "customer_unavailable", "damaged", "wrong_delivery", "delayed"]


# ---------------------------------------------------------------- evidence primitives
class Source(BaseModel):
    """Where a fact came from: file name + locator (row, line, timestamp, 'image')."""

    file: str
    locator: str

    def label(self) -> str:
        return f"{self.file} @ {self.locator}"


class TrackingRow(BaseModel):
    row: int
    timestamp: datetime
    status: str
    location: str = ""
    lat: float | None = None
    lon: float | None = None


class Claim(BaseModel):
    """Something the customer or driver asserts, normalised to a fixed type."""

    type: str
    text: str
    time: datetime | None = None
    source: Source


class VisionResult(BaseModel):
    door_visible: bool = False
    package_visible: bool = False
    gate_number: str | None = None
    damage_visible: bool = False
    scene: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class Transcript(BaseModel):
    text: str
    language: str = "en"


class Extraction(BaseModel):
    tracking: list[TrackingRow] = []
    vision: VisionResult | None = None
    vision_engine: str = "none"  # llm | cache | none | unavailable
    photo_file: str | None = None
    photo_time: datetime | None = None
    transcript: Transcript | None = None
    transcript_engine: str = "none"  # whisper | cache | typed | none | unavailable
    note_file: str | None = None
    note_time: datetime | None = None
    driver_claims: list[Claim] = []
    customer_claims: list[Claim] = []
    claims_engine: str = "none"
    meta: dict[str, Any] = {}


class Event(BaseModel):
    id: str = ""
    time: datetime | None
    kind: Literal["scan", "photo", "driver_note", "customer_message"]
    actor: Literal["system", "driver", "customer"]
    description: str
    source: Source
    data: dict[str, Any] = {}


class Contradiction(BaseModel):
    rule: str
    severity: Literal["high", "medium", "low"]
    summary: str
    evidence: list[Source]
    details: dict[str, Any] = {}


class CauseResult(BaseModel):
    cause: Cause | Literal["needs_review"]
    candidate: Cause | None = None
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reasoning: str
    evidence_ids: list[str] = []
    engine: str = "heuristic"


class NextAction(BaseModel):
    action: str
    owner: str
    priority: Literal["P1", "P2", "P3"]
    escalate_after_hours: int
    customer_facing: str
    triggered_by: list[str] = []


class GuardrailResult(BaseModel):
    ok: bool
    violations: list[dict[str, str]] = []


class TaskCard(BaseModel):
    title: str
    summary: str
    cause: str
    priority: str
    next_action: str
    owner: str
    escalate_after_hours: int
    checklist: list[str]
    evidence: list[str]


class Analysis(BaseModel):
    extraction: Extraction
    timeline: list[Event]
    contradictions: list[Contradiction]
    cause: CauseResult
    next_action: NextAction
    task_card: TaskCard
    customer_draft: str
    draft_engine: str
    guardrail: GuardrailResult
    unsupported_facts: list[str] = []


# ---------------------------------------------------------------- API models
class ShipmentSummary(BaseModel):
    id: str
    tracking_id: str
    status: str
    address: str
    cause: str | None
    confidence: float | None
    contradiction_count: int
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    draft_text: str
    final_text: str | None
    status: str
    has_audio: bool
    approved_by: str | None
    approved_at: datetime | None
    sent_at: datetime | None


class ShipmentDetail(ShipmentSummary):
    analysis: dict[str, Any] | None
    error: str | None
    messages: list[MessageOut]


class EditDraftIn(BaseModel):
    text: str
    editor: str = "dispatcher"


class ApproveIn(BaseModel):
    approver: str = "dispatcher"
    text: str | None = None  # optional final edit at approval time


class CheckTextIn(BaseModel):
    text: str


class FraudReportIn(BaseModel):
    description: str = Field(min_length=10, max_length=4000)


class AuditOut(BaseModel):
    id: int
    shipment_id: str | None
    actor: str
    action: str
    details: dict[str, Any]
    created_at: datetime
