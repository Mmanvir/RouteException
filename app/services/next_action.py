"""Next action = lookup in config/rules.yaml. Code only; the LLM never picks the action."""

from __future__ import annotations

from typing import Any

from app.schemas import CauseResult, Contradiction, NextAction

_PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3}


def decide_next_action(
    cause: CauseResult, contradictions: list[Contradiction], rules_cfg: dict[str, Any]
) -> NextAction:
    key = "needs_review" if cause.needs_review else cause.cause
    base = rules_cfg["actions"][key]
    priority = base["priority"]
    action = base["action"]
    triggered = [f"cause:{key}"]

    overrides = rules_cfg.get("contradiction_overrides", {})
    seen: set[str] = set()
    for c in contradictions:
        ov = overrides.get(c.rule)
        if not ov or c.rule in seen:
            continue
        seen.add(c.rule)
        triggered.append(f"rule:{c.rule}")
        if "priority" in ov and _PRIORITY_RANK[ov["priority"]] < _PRIORITY_RANK[priority]:
            priority = ov["priority"]
        if ov.get("extra_action"):
            action += f"; {ov['extra_action']}"

    return NextAction(
        action=action,
        owner=base["owner"],
        priority=priority,
        escalate_after_hours=int(base["escalate_after_hours"]),
        customer_facing=base["customer_facing"],
        triggered_by=triggered,
    )
