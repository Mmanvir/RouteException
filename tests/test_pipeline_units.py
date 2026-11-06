"""Timeline, next action, classifier, messages and the LLM helper."""

import json

from app.schemas import CauseResult, Contradiction, Source
from app.services import llm
from app.services.classifier import classify_cause
from app.services.messages import template_draft, unsupported_facts
from app.services.next_action import decide_next_action
from app.services.rules import run_rules
from app.services.timeline import build_timeline, expected_event_count


# ------------------------------------------------------------------ timeline
def test_timeline_sorted_tagged_and_complete(make_ex):
    ex = make_ex(customer=[("not_received", "never got it")], driver=["delivered"])
    tl = build_timeline(ex)
    times = [e.time for e in tl if e.time]
    assert times == sorted(times)
    assert [e.id for e in tl] == [f"E{i}" for i in range(1, len(tl) + 1)]
    assert all(e.source.file for e in tl)
    assert len(tl) == expected_event_count(ex)


def test_timeline_deterministic(make_ex):
    ex = make_ex(customer=[("late", "late")])
    assert [e.model_dump() for e in build_timeline(ex)] == [e.model_dump() for e in build_timeline(ex)]


# ------------------------------------------------------------------ next action
def _cause(c: str, review: bool = False) -> CauseResult:
    return CauseResult(cause="needs_review" if review else c, candidate=c, confidence=0.9 if not review else 0.3,
                       needs_review=review, reasoning="")


def test_next_action_comes_from_rules_yaml(rules_cfg):
    a = decide_next_action(_cause("delayed"), [], rules_cfg)
    assert a.action == rules_cfg["actions"]["delayed"]["action"]
    assert a.priority == "P3"


def test_next_action_override_raises_priority(rules_cfg):
    c = Contradiction(rule="gps_vs_address", severity="high", summary="", evidence=[Source(file="x", locator="y")])
    a = decide_next_action(_cause("delayed"), [c], rules_cfg)
    assert a.priority == "P1" and "GPS" in a.action


def test_next_action_needs_review(rules_cfg):
    a = decide_next_action(_cause("damaged", review=True), [], rules_cfg)
    assert a.action == rules_cfg["actions"]["needs_review"]["action"]


# ------------------------------------------------------------------ classifier
def test_heuristic_wrong_delivery(make_ex, rules_cfg):
    ex = make_ex(gate_seen="999", gps=(30.71, 76.70), customer=[("not_received", "never got it")])
    tl, cs = build_timeline(ex), run_rules(ex, rules_cfg)
    r = classify_cause(ex, tl, cs, rules_cfg)
    assert r.cause == "wrong_delivery" and not r.needs_review and r.evidence_ids


def test_heuristic_no_evidence_needs_review(make_ex, rules_cfg):
    ex = make_ex()
    r = classify_cause(ex, build_timeline(ex), [], rules_cfg)
    assert r.cause == "needs_review" and r.needs_review


def test_llm_cause_outside_taxonomy_goes_to_review(make_ex, rules_cfg, monkeypatch):
    from app.services import classifier

    def fake(task, prompt, schema, **kw):
        return schema(cause="driver_theft", confidence=0.99, reasoning="x", evidence_ids=["E1"]), "llm"

    monkeypatch.setattr(llm, "call_json", fake)
    ex = make_ex()
    r = classifier.classify_cause(ex, build_timeline(ex), [], rules_cfg)
    assert r.cause == "needs_review" and r.candidate is None


def test_llm_cause_with_invented_citations_goes_to_review(make_ex, rules_cfg, monkeypatch):
    def fake(task, prompt, schema, **kw):
        return schema(cause="damaged", confidence=0.95, reasoning="x", evidence_ids=["E99"]), "llm"

    monkeypatch.setattr(llm, "call_json", fake)
    ex = make_ex()
    r = classify_cause(ex, build_timeline(ex), [], rules_cfg)
    assert r.needs_review and r.evidence_ids == []


# ------------------------------------------------------------------ llm helper
def test_llm_retries_once_on_invalid_json(monkeypatch, tmp_path):
    from pydantic import BaseModel

    from app.config import get_settings

    class Out(BaseModel):
        x: int

    calls = []

    def fake_call(prompt, image, mime):
        calls.append(prompt)
        return "not json" if len(calls) == 1 else json.dumps({"x": 7})

    monkeypatch.setattr(llm, "provider_available", lambda: True)
    monkeypatch.setattr(llm, "_call_openai", fake_call)
    monkeypatch.setattr(llm, "_call_gemini", fake_call)
    monkeypatch.setattr(get_settings(), "llm_cache_dir", tmp_path)
    res = llm.call_json("t", "prompt", Out, cache_key="k1")
    assert res is not None and res[0].x == 7 and res[1] == "llm" and len(calls) == 2
    # second call is served from cache, no provider call
    assert llm.call_json("t", "prompt", Out, cache_key="k1")[1] == "cache" and len(calls) == 2


def test_llm_gives_up_after_one_retry(monkeypatch, tmp_path):
    from pydantic import BaseModel

    from app.config import get_settings

    class Out(BaseModel):
        x: int

    monkeypatch.setattr(llm, "provider_available", lambda: True)
    monkeypatch.setattr(llm, "_call_openai", lambda *a: "{bad")
    monkeypatch.setattr(llm, "_call_gemini", lambda *a: "{bad")
    monkeypatch.setattr(get_settings(), "llm_cache_dir", tmp_path)
    assert llm.call_json("t", "p", Out, cache_key="k2") is None


# ------------------------------------------------------------------ messages
def test_template_draft_is_neutral_and_supported(make_ex, rules_cfg):
    from app.services.guardrail import check_text
    from app.services.messages import private_terms

    ex = make_ex()
    for cause in ["address_issue", "customer_unavailable", "damaged", "wrong_delivery", "delayed"]:
        text = template_draft(ex, decide_next_action(_cause(cause), [], rules_cfg))
        assert check_text(text, private_terms(ex.meta)).ok
        assert unsupported_facts(text, ex) == []


def test_unsupported_fact_detected(make_ex):
    assert unsupported_facts("Your parcel will arrive on 14 Nov at 5pm, order RX999.", make_ex())
