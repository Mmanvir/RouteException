"""Evaluation on a labelled case folder (default: hidden test set).

    python scripts/evaluate.py --cases data/test_cases
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_rules  # noqa: E402
from app.services.evidence_pack import build_evidence_pack, is_complete  # noqa: E402
from app.services.extraction import PHOTO_NAMES  # noqa: E402
from app.services.pipeline import analyse_case  # noqa: E402

TARGETS = {
    "timeline_completeness": (">=", 0.95),
    "contradiction_recall": (">=", 0.90),
    "contradiction_precision": (">=", 0.85),
    "cause_accuracy": (">=", 0.80),
    "unsupported_claims": ("==", 0),
    "guardrail_violations_approved": ("==", 0),
    "gps_mismatch_detection": (">=", 0.90),
    "evidence_pack_completeness": (">=", 1.0),
}


def _expected_events(case: Path) -> int:
    """Counted straight from the raw files, independent of the extraction code."""
    with open(case / "tracking.csv", encoding="utf-8") as fh:
        n = sum(1 for _ in csv.DictReader(fh))
    n += any((case / p).exists() for p in PHOTO_NAMES)
    n += (case / "driver_note.txt").exists() or any(case.glob("driver_note.*"))
    if (case / "customer.txt").exists():
        n += sum(1 for ln in (case / "customer.txt").read_text(encoding="utf-8").splitlines() if ln.strip())
    return n


def evaluate(cases_dir: Path, labels_path: Path) -> dict[str, Any]:
    with open(labels_path, encoding="utf-8") as fh:
        labels = {r["case_id"]: r for r in csv.DictReader(fh)}
    case_dirs = sorted(d for d in cases_dir.iterdir() if (d / "meta.json").exists() and d.name in labels)
    if not case_dirs:
        raise SystemExit(f"no labelled cases in {cases_dir}")

    tl_got = tl_exp = tp = fp = fn = correct = unsupported = violations = 0
    gps_tp = gps_pos = packs_ok = 0
    per_case = []
    fraud_cfg = get_rules()["fraud"]

    with tempfile.TemporaryDirectory() as tmp:
        for case in case_dirs:
            lab = labels[case.name]
            a = analyse_case(case)
            exp = _expected_events(case)
            tl_got += min(len(a.timeline), exp)
            tl_exp += exp

            truth = {c for c in lab["contradictions"].split(";") if c}
            pred = {c.rule for c in a.contradictions}
            tp += len(truth & pred)
            fp += len(pred - truth)
            fn += len(truth - pred)

            ok_cause = a.cause.cause == lab["cause"]
            correct += ok_cause
            unsupported += bool(a.unsupported_facts)
            # Approval endpoint rejects any text failing the guardrail, so an approved message can only
            # violate if the draft that reaches approval does. We count drafts that would get through.
            violations += (not a.guardrail.ok)

            if lab["gps_mismatch"] == "1":
                gps_pos += 1
                gps_tp += "gps_vs_address" in pred

            photo = next((case / p for p in PHOTO_NAMES if (case / p).exists()), None)
            sections = build_evidence_pack(
                Path(tmp) / f"{case.name}.pdf",
                shipment={"id": case.name, "tracking_id": a.extraction.meta["tracking_id"],
                          "address": a.extraction.meta["address"]},
                timeline=[{"time": f"{r.timestamp:%Y-%m-%d %H:%M}", "status": r.status, "location": r.location}
                          for r in a.extraction.tracking],
                photo_path=photo, sent_messages=[{"sent_at": "-", "text": a.customer_draft}],
                report_text="Evaluation run.", fraud_cfg=fraud_cfg,
            )
            packs_ok += is_complete(sections)

            per_case.append({
                "case": case.name, "truth_cause": lab["cause"], "pred_cause": a.cause.cause,
                "confidence": a.cause.confidence, "cause_ok": ok_cause,
                "missed": sorted(truth - pred), "extra": sorted(pred - truth),
            })

    n = len(case_dirs)
    metrics = {
        "cases": n,
        "timeline_completeness": round(tl_got / tl_exp, 3) if tl_exp else 1.0,
        "contradiction_recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "contradiction_precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "cause_accuracy": round(correct / n, 3),
        "unsupported_claims": unsupported,
        "guardrail_violations_approved": violations,
        "gps_mismatch_detection": round(gps_tp / gps_pos, 3) if gps_pos else 1.0,
        "evidence_pack_completeness": round(packs_ok / n, 3),
    }
    return {"metrics": metrics, "per_case": per_case}


def passes(name: str, value: float) -> bool:
    op, target = TARGETS[name]
    return value >= target if op == ">=" else value == target


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "data" / "test_cases"))
    ap.add_argument("--labels", default=str(ROOT / "data" / "labels.csv"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = evaluate(Path(args.cases), Path(args.labels))
    if args.json:
        print(json.dumps(res, indent=2))
        return
    m = res["metrics"]
    print(f"\nRouteException evaluation - {m['cases']} cases from {args.cases}\n")
    print(f"{'metric':34} {'value':>8}  {'target':>8}  result")
    print("-" * 64)
    for k, (op, t) in TARGETS.items():
        v = m[k]
        shown = f"{v:.1%}" if isinstance(v, float) else str(v)
        tshown = f"{op}{t:.0%}" if isinstance(t, float) else f"{op}{t}"
        print(f"{k:34} {shown:>8}  {tshown:>8}  {'PASS' if passes(k, v) else 'FAIL'}")
    bad = [c for c in res["per_case"] if not c["cause_ok"] or c["missed"] or c["extra"]]
    if bad:
        print("\nCases with errors:")
        for c in bad:
            print(f"  {c['case']}: cause {c['pred_cause']} (truth {c['truth_cause']}, conf {c['confidence']})"
                  f" missed={c['missed']} extra={c['extra']}")


if __name__ == "__main__":
    main()
