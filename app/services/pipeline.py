"""Pipeline: extraction -> timeline -> rules -> cause -> next action -> messages -> guardrail."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from app.config import get_rules
from app.database import SessionLocal
from app.models import CustomerMessage, Shipment
from app.schemas import Analysis
from app.services.audit import log_event
from app.services.classifier import classify_cause
from app.services.extraction import extract_case
from app.services.messages import build_task_card, draft_customer_message
from app.services.next_action import decide_next_action
from app.services.rules import run_rules
from app.services.timeline import build_timeline

log = logging.getLogger("routeexception.pipeline")


def analyse_case(case_dir: Path) -> Analysis:
    """Pure function of the case folder (plus cached LLM output). Used by API, tests, evaluation."""
    rules_cfg = get_rules()
    ex = extract_case(case_dir)
    timeline = build_timeline(ex)
    contradictions = run_rules(ex, rules_cfg)
    cause = classify_cause(ex, timeline, contradictions, rules_cfg)
    action = decide_next_action(cause, contradictions, rules_cfg)
    card = build_task_card(ex, timeline, contradictions, cause, action)
    draft, engine, guard, unsupported = draft_customer_message(ex, cause, action)
    return Analysis(
        extraction=ex,
        timeline=timeline,
        contradictions=contradictions,
        cause=cause,
        next_action=action,
        task_card=card,
        customer_draft=draft,
        draft_engine=engine,
        guardrail=guard,
        unsupported_facts=unsupported,
    )


def process_shipment(shipment_id: str) -> None:
    """Background job: run the pipeline and move the shipment to needs_review."""
    db = SessionLocal()
    try:
        sh = db.get(Shipment, shipment_id)
        if sh is None:
            return
        try:
            a = analyse_case(Path(sh.case_dir))
        except Exception as e:  # keep the shipment visible to the dispatcher instead of losing it
            log.exception("pipeline failed for %s", shipment_id)
            sh.status = "failed"
            sh.error = f"{e}\n{traceback.format_exc(limit=3)}"
            db.commit()
            return
        sh.analysis = a.model_dump(mode="json")
        sh.cause = a.cause.cause
        sh.confidence = a.cause.confidence
        sh.contradiction_count = len(a.contradictions)
        sh.status = "needs_review"
        sh.error = None
        # A fresh analysis replaces any unsent draft; sent history is kept.
        for m in list(sh.messages):
            if m.status == "draft":
                db.delete(m)
        db.add(CustomerMessage(shipment_id=sh.id, draft_text=a.customer_draft, status="draft"))
        log_event(db, sh.id, "system", "pipeline_completed", {
            "cause": a.cause.cause, "confidence": a.cause.confidence,
            "contradictions": [c.rule for c in a.contradictions], "draft_engine": a.draft_engine,
        })
        db.commit()
    finally:
        db.close()
