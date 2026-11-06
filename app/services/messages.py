"""Task card (code + optional LLM summary) and neutral customer draft (LLM or template).

Every draft goes through guardrail.check_text and an unsupported-fact check before a
dispatcher ever sees it. If the LLM draft fails either check we fall back to the safe template.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.schemas import CauseResult, Contradiction, Event, Extraction, GuardrailResult, NextAction, TaskCard
from app.services.guardrail import check_text
from app.services.llm import call_json

_CAUSE_LABEL = {
    "address_issue": "Address issue",
    "customer_unavailable": "Customer unavailable",
    "damaged": "Damaged parcel",
    "wrong_delivery": "Possible wrong delivery",
    "delayed": "Delayed",
    "needs_review": "Needs review",
}


def private_terms(meta: dict[str, Any]) -> list[str]:
    """Things that must never appear in customer-facing text."""
    d = meta.get("driver") or {}
    customer = (meta.get("customer_first_name") or "").strip().lower()
    terms = [d.get("name", ""), d.get("phone", ""), d.get("id", "")]
    # Individual name tokens too, except one that is also the customer's own name.
    terms += [tok for tok in (d.get("name", "") or "").split() if tok.lower() != customer]
    return [t for t in terms if t and len(t) >= 3]


def allowed_facts(ex: Extraction) -> set[str]:
    """Numbers/dates the customer message is allowed to mention (from evidence only)."""
    facts = {ex.meta.get("tracking_id", ""), ex.meta.get("shipment_id", "")}
    for r in ex.tracking:
        facts |= {r.timestamp.strftime(f) for f in ("%d %b", "%d %B", "%Y-%m-%d", "%H:%M", "%d")}
    return {f for f in facts if f}


_NUMBERISH = re.compile(r"\b[A-Z]{0,4}-?\d[\w:-]*\b")


def unsupported_facts(text: str, ex: Extraction) -> list[str]:
    """Any number/ID/time in the text that does not come from the evidence is unsupported."""
    allowed = allowed_facts(ex)
    bad = []
    for tok in _NUMBERISH.findall(text):
        if tok in allowed or tok.lstrip("0") in allowed:
            continue
        if any(tok in a for a in allowed):
            continue
        bad.append(tok)
    return bad


def template_draft(ex: Extraction, action: NextAction) -> str:
    name = (ex.meta.get("customer_first_name") or "").strip()
    greet = f"Hello {name}," if name else "Hello,"
    tid = ex.meta.get("tracking_id", "")
    return (
        f"{greet} thank you for contacting us about your parcel {tid}. "
        f"Our team is looking into it, and {action.customer_facing}. "
        "Our team will share the next update with you here. We appreciate your patience."
    )


class _Draft(BaseModel):
    message: str = Field(max_length=700)


class _Summary(BaseModel):
    summary: str = Field(max_length=500)


def draft_customer_message(ex: Extraction, cause: CauseResult, action: NextAction) -> tuple[str, str, GuardrailResult, list[str]]:
    terms = private_terms(ex.meta)
    prompt = (
        "Write a short (max 70 words), calm, neutral update to a customer about their parcel "
        f"{ex.meta.get('tracking_id')}. Greet them as {ex.meta.get('customer_first_name', 'there')}.\n"
        f"What we are doing (say this, in your own words): {action.customer_facing}.\n"
        "Rules: do not blame anyone; do not promise refunds, compensation, vouchers or dates; "
        "do not mention the driver, phone numbers, GPS or locations; do not state any fact "
        'not given here. Return {"message": str}.'
    )
    res = call_json("customer_draft", prompt, _Draft)
    if res is not None:
        text, engine = res[0].message.strip(), res[1]
        g = check_text(text, terms)
        bad = unsupported_facts(text, ex)
        if g.ok and not bad:
            return text, engine, g, bad
    text = template_draft(ex, action)
    return text, "template", check_text(text, terms), unsupported_facts(text, ex)


def build_task_card(ex: Extraction, timeline: list[Event], contradictions: list[Contradiction],
                    cause: CauseResult, action: NextAction) -> TaskCard:
    sid = ex.meta.get("shipment_id", "")
    label = _CAUSE_LABEL.get(cause.cause, cause.cause)
    if cause.needs_review and cause.candidate:
        label = f"Needs review (likely {_CAUSE_LABEL[cause.candidate].lower()})"

    by_id = {e.id: e for e in timeline}
    cited = [f"{i}: {by_id[i].description} [{by_id[i].source.label()}]" for i in cause.evidence_ids if i in by_id]
    cited += [f"{c.rule}: {c.summary}" for c in contradictions]

    checklist = [s.strip() for s in action.action.split(";") if s.strip()]
    if cause.needs_review:
        checklist.insert(0, "Confirm or correct the cause before approving the customer update")
    checklist.append(f"Escalate if unresolved after {action.escalate_after_hours} h")

    fallback_summary = (
        f"{len(contradictions)} contradiction(s) found. {cause.reasoning}"
        if contradictions else cause.reasoning
    )
    prompt = (
        "Summarise this delivery exception for a dispatcher in 2 neutral sentences. "
        "Use only these facts, no blame:\n"
        f"Cause: {label}\nContradictions: {[c.summary for c in contradictions]}\n"
        f"Reasoning: {cause.reasoning}\n" 'Return {"summary": str}.'
    )
    res = call_json("task_summary", prompt, _Summary)
    summary = fallback_summary
    if res is not None and check_text(res[0].summary).ok:
        summary = res[0].summary

    return TaskCard(
        title=f"{sid} · {label}",
        summary=summary,
        cause=cause.cause,
        priority=action.priority,
        next_action=action.action,
        owner=action.owner,
        escalate_after_hours=action.escalate_after_hours,
        checklist=checklist,
        evidence=cited,
    )
