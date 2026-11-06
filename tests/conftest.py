from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="rx-test-"))
os.environ.update({
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "STORAGE_DIR": str(_TMP / "storage"),
    "TTS_ENABLED": "false",
    "LLM_PROVIDER": "offline",
})

if not (ROOT / "data" / "cases" / "SHP-1001" / "meta.json").exists():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_data.py"), "--count", "18"], check=True)

from app.config import get_rules  # noqa: E402
from app.schemas import Claim, Extraction, Source, TrackingRow, VisionResult  # noqa: E402


@pytest.fixture
def rules_cfg() -> dict:
    return get_rules()


def T(s: str) -> datetime:
    return datetime.fromisoformat(s)


@pytest.fixture
def make_ex():
    """Build an Extraction by hand. Default: clean delivered case at the address."""

    def _make(final_status: str = "delivered", gps=(30.70000, 76.70000), photo: bool = True,
              gate_seen: str | None = "214", door: bool = True, customer: list[tuple[str, str]] = (),
              driver: list[str] = (), note: bool = False, gap_hours: float = 2, photo_delay_min: float = 1,
              ) -> Extraction:
        rows = [
            TrackingRow(row=1, timestamp=T("2026-10-01 09:00"), status="created", location="Hub"),
            TrackingRow(row=2, timestamp=T("2026-10-01 11:00"), status="picked_up", location="Hub"),
        ]
        t3 = T("2026-10-01 11:00").timestamp() + gap_hours * 3600
        rows.append(TrackingRow(row=3, timestamp=datetime.fromtimestamp(t3), status="out_for_delivery"))
        final_t = datetime.fromtimestamp(t3 + 3600)
        if final_status:
            rows.append(TrackingRow(row=4, timestamp=final_t, status=final_status, location="Lotus Vihar",
                                    lat=gps[0] if gps else None, lon=gps[1] if gps else None))
        ex = Extraction(
            tracking=rows,
            meta={"shipment_id": "SHP-T", "tracking_id": "RXT1", "gate_number": "214",
                  "address_lat": 30.70000, "address_lon": 76.70000, "customer_first_name": "Asha",
                  "driver": {"name": "Ravi Kumar", "phone": "9000012345", "id": "DRV-1"}},
        )
        if photo:
            ex.photo_file = "pod.jpg"
            ex.photo_time = datetime.fromtimestamp(final_t.timestamp() + photo_delay_min * 60)
            ex.vision = VisionResult(door_visible=door, package_visible=True, gate_number=gate_seen)
        if note or driver:
            ex.note_file = "driver_note.txt"
        ex.customer_claims = [Claim(type=t, text=x, source=Source(file="customer.txt", locator=f"line {i}"))
                              for i, (t, x) in enumerate(customer, start=1)]
        ex.driver_claims = [Claim(type=t, text=t, source=Source(file="driver_note.txt", locator="sentence 1"))
                            for t in driver]
        return ex

    return _make
