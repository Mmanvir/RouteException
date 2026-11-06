"""Contradiction rules. Code only, deterministic: same input -> same output."""

from __future__ import annotations

import math
import re
from typing import Any

from app.schemas import Contradiction, Extraction, Source, TrackingRow

NOT_RECEIVED = {"not_received"}
DRIVER_NOT_DELIVERED = {"address_not_found", "customer_absent", "not_delivered", "returned"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalise_gate(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()
    digits = re.sub(r"^(GATE|G|NO|NUMBER|HOUSE|H)+", "", digits)
    return digits or None


def final_scan(ex: Extraction, terminal: list[str]) -> TrackingRow | None:
    rows = [r for r in ex.tracking if r.status in terminal]
    return max(rows, key=lambda r: (r.timestamp, r.row)) if rows else None


def _src(r: TrackingRow) -> Source:
    return Source(file="tracking.csv", locator=f"row {r.row}")


def check_status_vs_customer(ex: Extraction, final: TrackingRow | None) -> list[Contradiction]:
    if not final:
        return []
    out = []
    if final.status == "delivered":
        for c in ex.customer_claims:
            if c.type in NOT_RECEIVED:
                out.append(
                    Contradiction(
                        rule="status_vs_customer",
                        severity="high",
                        summary="Tracking says delivered, customer says the parcel was not received.",
                        evidence=[_src(final), c.source],
                    )
                )
                break
    if final.status == "attempted":
        for c in ex.customer_claims:
            if c.type == "customer_was_home":
                out.append(
                    Contradiction(
                        rule="status_vs_customer",
                        severity="medium",
                        summary="Tracking says delivery was attempted, customer says they were available.",
                        evidence=[_src(final), c.source],
                    )
                )
                break
    return out


def check_photo_vs_address(ex: Extraction, final: TrackingRow | None) -> list[Contradiction]:
    if not ex.vision or not ex.photo_file:
        return []
    src = Source(file=ex.photo_file, locator="image")
    expected = normalise_gate(ex.meta.get("gate_number"))
    seen = normalise_gate(ex.vision.gate_number)
    out = []
    if expected and seen and expected != seen:
        out.append(
            Contradiction(
                rule="photo_vs_address",
                severity="high",
                summary=f"Photo shows gate {ex.vision.gate_number}, registered address is gate "
                f"{ex.meta.get('gate_number')}.",
                evidence=[src, Source(file="meta.json", locator="gate_number")],
                details={"expected": expected, "seen": seen},
            )
        )
    elif final and final.status == "delivered" and not ex.vision.door_visible:
        out.append(
            Contradiction(
                rule="photo_vs_address",
                severity="medium",
                summary="Photo does not show a door or gate, so it cannot confirm the address.",
                evidence=[src],
            )
        )
    return out


def check_status_vs_driver_note(ex: Extraction, final: TrackingRow | None) -> list[Contradiction]:
    if not final or not ex.driver_claims:
        return []
    types = {c.type for c in ex.driver_claims}
    note_src = ex.driver_claims[0].source
    if final.status == "delivered" and types & DRIVER_NOT_DELIVERED:
        return [
            Contradiction(
                rule="status_vs_driver_note",
                severity="high",
                summary="Marked delivered, but the driver's voice note says it was not handed over "
                f"({', '.join(sorted(types & DRIVER_NOT_DELIVERED))}).",
                evidence=[_src(final), note_src],
            )
        ]
    if final.status == "attempted" and "delivered" in types:
        return [
            Contradiction(
                rule="status_vs_driver_note",
                severity="high",
                summary="Marked attempted, but the driver's voice note says it was delivered.",
                evidence=[_src(final), note_src],
            )
        ]
    if final.status == "delivered" and "damaged" in types:
        return [
            Contradiction(
                rule="status_vs_driver_note",
                severity="medium",
                summary="Marked delivered without a damage flag, but the driver's note mentions damage.",
                evidence=[_src(final), note_src],
            )
        ]
    return []


def check_time_gap(ex: Extraction, final: TrackingRow | None, th: dict[str, Any]) -> list[Contradiction]:
    out: list[Contradiction] = []
    rows = sorted(ex.tracking, key=lambda r: (r.timestamp, r.row))
    max_h = float(th["max_scan_gap_hours"])
    for a, b in zip(rows, rows[1:]):
        gap_h = (b.timestamp - a.timestamp).total_seconds() / 3600
        if gap_h > max_h:
            out.append(
                Contradiction(
                    rule="time_gap",
                    severity="medium",
                    summary=f"{gap_h:.1f} h with no scan between '{a.status}' and '{b.status}' "
                    f"(limit {max_h:.0f} h).",
                    evidence=[_src(a), _src(b)],
                    details={"gap_hours": round(gap_h, 2)},
                )
            )
            break
    if final and ex.photo_time and ex.photo_file:
        gap_m = abs((ex.photo_time - final.timestamp).total_seconds()) / 60
        if gap_m > float(th["photo_scan_gap_minutes"]):
            out.append(
                Contradiction(
                    rule="time_gap",
                    severity="medium",
                    summary=f"Photo was taken {gap_m:.0f} min away from the '{final.status}' scan.",
                    evidence=[_src(final), Source(file=ex.photo_file, locator="image")],
                    details={"gap_minutes": round(gap_m, 1)},
                )
            )
    return out


def check_missing_evidence(ex: Extraction, final: TrackingRow | None) -> list[Contradiction]:
    if not final or final.status not in {"delivered", "attempted"}:
        return []
    missing = []
    if not ex.photo_file:
        missing.append("delivery photo")
    if final.lat is None or final.lon is None:
        missing.append("GPS on the final scan")
    if final.status == "attempted" and not ex.note_file:
        missing.append("driver note for the failed attempt")
    if not missing:
        return []
    return [
        Contradiction(
            rule="missing_evidence",
            severity="medium",
            summary=f"Status '{final.status}' has no {', no '.join(missing)}.",
            evidence=[_src(final)],
            details={"missing": missing},
        )
    ]


def check_gps_vs_address(ex: Extraction, final: TrackingRow | None, th: dict[str, Any]) -> list[Contradiction]:
    lat, lon = ex.meta.get("address_lat"), ex.meta.get("address_lon")
    if not final or final.lat is None or final.lon is None or lat is None or lon is None:
        return []
    d = haversine_m(final.lat, final.lon, float(lat), float(lon))
    limit = float(th["gps_mismatch_meters"])
    if d <= limit:
        return []
    return [
        Contradiction(
            rule="gps_vs_address",
            severity="high",
            summary=f"'{final.status}' scan was {d:,.0f} m from the delivery address (limit {limit:.0f} m).",
            evidence=[_src(final), Source(file="meta.json", locator="address_lat/address_lon")],
            details={"distance_m": round(d, 1)},
        )
    ]


def run_rules(ex: Extraction, rules_cfg: dict[str, Any]) -> list[Contradiction]:
    th = rules_cfg["thresholds"]
    final = final_scan(ex, rules_cfg["terminal_statuses"])
    found: list[Contradiction] = []
    found += check_status_vs_customer(ex, final)
    found += check_photo_vs_address(ex, final)
    found += check_status_vs_driver_note(ex, final)
    found += check_time_gap(ex, final, th)
    found += check_missing_evidence(ex, final)
    found += check_gps_vs_address(ex, final, th)
    return found
