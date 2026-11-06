"""Cause classification. LLM picks exactly one cause from a fixed taxonomy and cites evidence ids.

Code validates the answer: unknown cause, no valid citations, or low confidence -> needs_review.
Without an LLM a transparent, deterministic evidence-scoring fallback is used.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.schemas import CAUSES, CauseResult, Contradiction, Event, Extraction
from app.services.rules import final_scan


class _LLMCause(BaseModel):
    cause: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str
    evidence_ids: list[str]


def evidence_items(timeline: list[Event], contradictions: list[Contradiction]) -> dict[str, str]:
    items = {e.id: f"[{e.time:%Y-%m-%d %H:%M}] {e.description}" if e.time else e.description for e in timeline}
    for i, c in enumerate(contradictions, start=1):
        items[f"C{i}"] = f"CONTRADICTION {c.rule}: {c.summary}"
    return items


def _heuristic(ex: Extraction, timeline: list[Event], contradictions: list[Contradiction],
               rules_cfg: dict[str, Any]) -> tuple[str, float, str, list[str]]:
    """Weighted evidence vote. Every point is tied to an evidence id so it can be cited."""
    score = {c: 0.0 for c in CAUSES}
    cites: dict[str, list[str]] = {c: [] for c in CAUSES}
    why: dict[str, list[str]] = {c: [] for c in CAUSES}

    def vote(cause: str, w: float, eid: str, reason: str) -> None:
        score[cause] += w
        if eid not in cites[cause]:
            cites[cause].append(eid)
        why[cause].append(reason)

    final = final_scan(ex, rules_cfg["terminal_statuses"])
    rules_hit = {c.rule: f"C{i}" for i, c in reversed(list(enumerate(contradictions, start=1)))}

    for e in timeline:
        claim = e.data.get("claim")
        if e.kind == "customer_message":
            if claim == "damaged":
                vote("damaged", 2.0, e.id, "customer reports damage")
            elif claim == "wrong_item":
                vote("wrong_delivery", 2.0, e.id, "customer received the wrong item")
            elif claim == "late":
                vote("delayed", 1.5, e.id, "customer reports a delay")
            elif claim == "address_problem":
                vote("address_issue", 1.5, e.id, "customer mentions an address problem")
            elif claim == "customer_was_home":
                vote("customer_unavailable", 1.0, e.id, "dispute about customer availability")
            elif claim == "not_received":
                if final and final.status == "delivered":
                    vote("wrong_delivery", 1.0, e.id, "marked delivered but not received")
                else:
                    vote("delayed", 1.0, e.id, "customer still waiting for the parcel")
        elif e.kind == "driver_note":
            for t in e.data.get("claims", []):
                if t == "address_not_found":
                    vote("address_issue", 2.0, e.id, "driver could not find the address")
                elif t == "customer_absent":
                    vote("customer_unavailable", 2.0, e.id, "driver says nobody was available")
                elif t == "damaged":
                    vote("damaged", 2.0, e.id, "driver mentions damage")
        elif e.kind == "photo" and e.data.get("damage_visible"):
            vote("damaged", 2.0, e.id, "photo shows a damaged parcel")
        elif e.kind == "scan" and e.data.get("status") == "damaged_reported":
            vote("damaged", 2.0, e.id, "damage scan in tracking")

    if "gps_vs_address" in rules_hit:
        vote("wrong_delivery", 1.5, rules_hit["gps_vs_address"], "delivery scan far from the address")
    if "photo_vs_address" in rules_hit:
        vote("wrong_delivery", 1.5, rules_hit["photo_vs_address"], "photo gate does not match the address")
    if "time_gap" in rules_hit:
        vote("delayed", 1.5, rules_hit["time_gap"], "long gap between scans")

    promised = ex.meta.get("promised_by")
    if promised and ex.tracking:
        from pandas import to_datetime

        last = max(ex.tracking, key=lambda r: r.timestamp)
        p = to_datetime(promised).to_pydatetime()
        if (final is None or final.timestamp > p) and last.timestamp > p:
            eid = next((e.id for e in timeline if e.kind == "scan"
                        and e.source.locator == f"row {last.row}"), timeline[-1].id)
            vote("delayed", 1.0, eid, "past the promised delivery date")

    if final and final.status == "attempted" and not any(score[c] for c in ("address_issue", "customer_unavailable")):
        eid = next(e.id for e in timeline if e.source.locator == f"row {final.row}" and e.kind == "scan")
        vote("customer_unavailable", 0.5, eid, "failed attempt recorded")

    total = sum(score.values())
    if total == 0:
        return "needs_review", 0.0, "No evidence points to any cause.", []
    ranked = sorted(CAUSES, key=lambda c: (-score[c], CAUSES.index(c)))
    best = ranked[0]
    conf = score[best] / total
    if score[ranked[1]] == score[best]:
        conf = min(conf, 0.5)  # a tie is never confident
    reasoning = "; ".join(dict.fromkeys(why[best])).capitalize() + "."
    return best, round(conf, 2), reasoning, cites[best]


def classify_cause(ex: Extraction, timeline: list[Event], contradictions: list[Contradiction],
                   rules_cfg: dict[str, Any]) -> CauseResult:
    from app.services.llm import call_json

    threshold = float(rules_cfg["thresholds"]["cause_confidence_threshold"])
    items = evidence_items(timeline, contradictions)
    prompt = (
        "You classify the cause of a last-mile delivery exception.\n"
        f"Choose exactly ONE cause from: {list(CAUSES)}.\n"
        "Base your answer only on the evidence below. Cite the evidence ids you relied on.\n"
        "If the evidence is weak or conflicting, give a low confidence.\n\n"
        "Evidence:\n" + json.dumps(items, indent=1, default=str) +
        '\n\nReturn {"cause": str, "confidence": 0..1, "reasoning": "<=2 neutral sentences", "evidence_ids": [str]}'
    )
    res = call_json("cause", prompt, _LLMCause)

    if res is not None:
        out, engine = res
        valid_ids = [i for i in out.evidence_ids if i in items]
        cause_ok = out.cause in CAUSES
        conf = out.confidence if (cause_ok and valid_ids) else 0.0
        candidate = out.cause if cause_ok else None
        reasoning = out.reasoning if cause_ok else f"Model returned '{out.cause}', outside the taxonomy."
    else:
        engine = "heuristic"
        cause, conf, reasoning, valid_ids = _heuristic(ex, timeline, contradictions, rules_cfg)
        candidate = cause if cause in CAUSES else None

    needs_review = candidate is None or conf < threshold
    return CauseResult(
        cause="needs_review" if needs_review else candidate,  # type: ignore[arg-type]
        candidate=candidate,  # type: ignore[arg-type]
        confidence=round(conf, 2),
        needs_review=needs_review,
        reasoning=reasoning,
        evidence_ids=valid_ids,
        engine=engine,
    )
