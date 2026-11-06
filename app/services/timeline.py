"""Merge all extracted events into one time-ordered, source-tagged timeline. Code only."""

from __future__ import annotations

from datetime import datetime

from app.schemas import Event, Extraction, Source

_KIND_ORDER = {"scan": 0, "photo": 1, "driver_note": 2, "customer_message": 3}


def build_timeline(ex: Extraction) -> list[Event]:
    events: list[Event] = []

    for r in ex.tracking:
        where = f" at {r.location}" if r.location else ""
        events.append(
            Event(
                time=r.timestamp,
                kind="scan",
                actor="driver" if r.status in {"delivered", "attempted", "out_for_delivery"} else "system",
                description=f"Status '{r.status}'{where}",
                source=Source(file="tracking.csv", locator=f"row {r.row}"),
                data={"status": r.status, "lat": r.lat, "lon": r.lon, "location": r.location},
            )
        )

    if ex.photo_file:
        if ex.vision:
            v = ex.vision
            desc = (
                f"Delivery photo: {v.scene or 'scene'}; gate {v.gate_number or 'not readable'}; "
                f"package {'visible' if v.package_visible else 'not visible'}"
                + ("; damage visible" if v.damage_visible else "")
            )
        else:
            desc = "Delivery photo uploaded (not yet analysed)"
        events.append(
            Event(
                time=ex.photo_time,
                kind="photo",
                actor="driver",
                description=desc,
                source=Source(file=ex.photo_file, locator="image"),
                data=ex.vision.model_dump() if ex.vision else {},
            )
        )

    if ex.note_file and ex.transcript:
        events.append(
            Event(
                time=ex.note_time,
                kind="driver_note",
                actor="driver",
                description=f'Driver note: "{ex.transcript.text}"',
                source=Source(file=ex.note_file, locator="transcript"),
                data={"claims": [c.type for c in ex.driver_claims]},
            )
        )

    for c in ex.customer_claims:
        events.append(
            Event(
                time=c.time,
                kind="customer_message",
                actor="customer",
                description=f'Customer: "{c.text}"',
                source=c.source,
                data={"claim": c.type},
            )
        )

    # Deterministic order: time (None last), then kind, then source locator.
    events.sort(
        key=lambda e: (
            e.time is None,
            e.time or datetime.max,
            _KIND_ORDER[e.kind],
            e.source.file,
            e.source.locator,
        )
    )
    for i, e in enumerate(events, start=1):
        e.id = f"E{i}"
    return events


def expected_event_count(ex: Extraction) -> int:
    """Number of events that should appear on the timeline (used by evaluation)."""
    return (
        len(ex.tracking)
        + (1 if ex.photo_file else 0)
        + (1 if ex.note_file and ex.transcript else 0)
        + len(ex.customer_claims)
    )
