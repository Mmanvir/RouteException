"""Synthetic shipment generator. Synthetic data only: fake names, fictional localities, fake gate numbers.

Each case folder:  meta.json, tracking.csv, pod.jpg (door/gate with printed number), driver_note.txt
(stored Whisper transcript; --audio also writes driver_note.mp3), customer.txt.

The vision output the generator *knows* is true for each image is seeded into data/llm_cache so the
demo runs offline. With an LLM provider configured you can delete the cache and use live vision.

    python scripts/generate_data.py --count 40            # data/cases + 10 hidden data/test_cases
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from faker import Faker
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.schemas import Transcript, VisionResult  # noqa: E402
from app.services.llm import seed_cache  # noqa: E402

LOCALITIES = ["Greenfield Enclave", "Lotus Vihar", "Riverside Block", "Sunrise Colony", "Maple Nagar",
              "Orchid Residency", "Kesar Gardens", "Silver Oak Phase 2"]
CITY = "Navpur"  # fictional city
BASE_LAT, BASE_LON = 30.7046, 76.7179  # coordinates are synthetic anchors only


@dataclass
class Case:
    cause: str
    contradictions: set[str]
    final_status: str | None
    gps_far_m: float = 0
    photo: dict | None = field(default_factory=lambda: {"gate": "match", "package": True, "damage": False, "door": True})
    note: str | None = None
    customer: list[str] = field(default_factory=list)
    gap_hours: float = 0  # stuck in transit
    late: bool = False
    gps_missing: bool = False
    pre_terminal: list[str] = field(default_factory=list)


def scenarios() -> list[Case]:
    nr = "The app says delivered but I never received my parcel."
    return [
        Case("wrong_delivery", {"status_vs_customer", "gps_vs_address", "photo_vs_address"}, "delivered",
             gps_far_m=900, photo={"gate": "other", "package": True, "damage": False, "door": True},
             note="Delivered the parcel, handed it to the person at the gate.",
             customer=[nr, "Please check where it was left, I have asked my neighbours."]),
        Case("wrong_delivery", {"status_vs_customer", "photo_vs_address"}, "delivered",
             photo={"gate": "other", "package": True, "damage": False, "door": True},
             customer=[nr]),
        Case("wrong_delivery", {"status_vs_customer", "gps_vs_address"}, "delivered", gps_far_m=650,
             note="Left the parcel with the security guard.", customer=[nr]),
        Case("wrong_delivery", set(), "delivered",
             customer=["I received the wrong item, this is not what I ordered."]),
        Case("wrong_delivery", {"status_vs_customer", "photo_vs_address"}, "delivered",
             photo={"gate": None, "package": True, "damage": False, "door": False}, customer=[nr]),
        Case("wrong_delivery", {"status_vs_customer", "missing_evidence"}, "delivered", photo=None,
             customer=[nr]),
        Case("customer_unavailable", set(), "attempted",
             photo={"gate": "match", "package": False, "damage": False, "door": True},
             note="Nobody answered the door and the gate was locked. Will try again tomorrow.",
             customer=["Can you deliver tomorrow evening?"]),
        Case("customer_unavailable", {"status_vs_customer"}, "attempted",
             photo={"gate": "match", "package": False, "damage": False, "door": True},
             note="Nobody answered the bell, customer phone was not answering.",
             customer=["I was home all day and nobody rang the bell."]),
        Case("customer_unavailable", {"missing_evidence"}, "attempted", photo=None, note=None,
             customer=["I was at work, please try again on Saturday."]),
        Case("address_issue", {"gps_vs_address"}, "attempted", gps_far_m=380,
             photo={"gate": None, "package": False, "damage": False, "door": True},
             note="I could not find the address, there is no gate number on the building.",
             customer=["The landmark is the blue temple next to my lane."]),
        Case("address_issue", set(), "attempted",
             photo={"gate": "match", "package": False, "damage": False, "door": True},
             note="Unable to locate the flat, the address was incomplete.",
             customer=["My address is correct, please deliver it."]),
        Case("address_issue", {"status_vs_customer", "status_vs_driver_note"}, "delivered",
             note="Couldn't find the address so I am returning it to the hub.", customer=[nr]),
        Case("damaged", set(), "delivered",
             photo={"gate": "match", "package": True, "damage": True, "door": True},
             customer=["The box arrived crushed and the item inside is broken."]),
        Case("damaged", {"status_vs_driver_note"}, "delivered",
             note="The box was already damaged when I picked it up from the hub.",
             customer=["The item inside is broken."]),
        Case("damaged", set(), "returned_to_hub", photo=None, pre_terminal=["damaged_reported"],
             customer=["Where is my parcel? It was supposed to come yesterday."]),
        Case("delayed", {"time_gap"}, None, gap_hours=52, photo=None, late=True,
             customer=["I am still waiting, it was promised two days ago."]),
        Case("delayed", {"time_gap"}, "delivered", gap_hours=34, late=True,
             customer=["It arrived very late, three days after the promised date."]),
        Case("delayed", set(), None, photo=None, late=True, customer=["When will my parcel arrive?"]),
    ]


# ------------------------------------------------------------------ helpers
def offset(lat: float, lon: float, meters: float, bearing_deg: float) -> tuple[float, float]:
    dlat = meters * math.cos(math.radians(bearing_deg)) / 111_320
    dlon = meters * math.sin(math.radians(bearing_deg)) / (111_320 * math.cos(math.radians(lat)))
    return round(lat + dlat, 6), round(lon + dlon, 6)


def font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def draw_pod(rng: random.Random, gate_text: str | None, door: bool, package: bool, damage: bool) -> bytes:
    w, h = 960, 720
    wall = rng.choice([(214, 196, 170), (190, 200, 205), (225, 214, 190), (200, 185, 175)])
    img = Image.new("RGB", (w, h), wall)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 48):  # brick hint
        d.line([(0, y), (w, y)], fill=tuple(max(c - 18, 0) for c in wall), width=2)
    d.rectangle([0, int(h * 0.82), w, h], fill=(120, 120, 118))  # floor
    if door:
        col = rng.choice([(92, 58, 34), (40, 70, 110), (60, 90, 60), (70, 70, 75)])
        x0, x1 = 300, 660
        d.rectangle([x0 - 16, 90, x1 + 16, int(h * 0.82)], fill=(60, 60, 60))
        d.rectangle([x0, 106, x1, int(h * 0.82)], fill=col)
        for gx in range(x0 + 30, x1, 44):  # gate bars
            d.line([(gx, 120), (gx, int(h * 0.80))], fill=tuple(min(c + 35, 255) for c in col), width=6)
        d.ellipse([x1 - 50, 360, x1 - 30, 380], fill=(210, 180, 60))
    else:
        d.rectangle([0, int(h * 0.55), w, int(h * 0.82)], fill=(150, 140, 130))  # just a ledge/stairs
    if gate_text:
        d.rounded_rectangle([700, 150, 900, 250], radius=10, fill=(250, 250, 245), outline=(20, 20, 20), width=4)
        f = font(54)
        tw = d.textlength(gate_text, font=f)
        d.text((800 - tw / 2, 170), gate_text, fill=(15, 15, 15), font=f)
    if package:
        bx, by = 360 if door else 400, int(h * 0.82) - 130
        d.rectangle([bx, by, bx + 190, by + 130], fill=(176, 132, 82), outline=(110, 80, 45), width=3)
        d.rectangle([bx + 80, by, bx + 110, by + 130], fill=(205, 175, 120))
        if damage:
            d.polygon([(bx, by + 10), (bx + 60, by + 40), (bx + 20, by + 70), (bx + 70, by + 120), (bx, by + 130)],
                      fill=(130, 95, 55))
            for _ in range(7):
                x, y = bx + rng.randint(10, 180), by + rng.randint(10, 120)
                d.line([(x, y), (x + rng.randint(-40, 40), y + rng.randint(-30, 30))], fill=(70, 50, 30), width=4)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def write_case(out_root: Path, idx: int, sc: Case, rng: random.Random, fake: Faker, audio: bool) -> dict:
    sid, tid = f"SHP-{idx}", f"RX{idx + 9000}"  # SHP-1001 -> RX10001
    case = out_root / sid
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)

    gate_no = str(rng.randint(10, 899))
    lat, lon = offset(BASE_LAT, BASE_LON, rng.uniform(200, 6000), rng.uniform(0, 360))
    locality = rng.choice(LOCALITIES)
    first = fake.first_name()
    driver = {"id": f"DRV-{rng.randint(100, 999)}", "name": fake.name(),
              "phone": f"90000{rng.randint(10000, 99999)}"}  # fake, never shown to customers

    day0 = datetime(2026, 10, 1, 9, 0) + timedelta(days=rng.randint(0, 25), minutes=rng.randint(0, 90))
    rows: list[list] = [
        [day0, "created", f"{CITY} Sort Centre", "", ""],
        [day0 + timedelta(hours=2, minutes=rng.randint(0, 40)), "picked_up", f"{CITY} Sort Centre", "", ""],
        [day0 + timedelta(hours=8, minutes=rng.randint(0, 50)), "in_transit", f"{locality} Hub", "", ""],
    ]
    ofd = day0 + timedelta(days=1, hours=-0.5 + rng.random()) + timedelta(hours=sc.gap_hours)
    rows.append([ofd, "out_for_delivery", f"{locality} Hub", "", ""])
    promised = day0 + timedelta(days=1, hours=10)
    if sc.late:
        promised = day0 + timedelta(hours=20)

    t = ofd + timedelta(hours=rng.uniform(1.5, 5))
    for st in sc.pre_terminal:
        rows.append([t, st, f"{locality}", "", ""])
        t += timedelta(minutes=rng.randint(20, 60))
    final_time = None
    if sc.final_status:
        final_time = t
        if sc.gps_missing:
            glat = glon = ""
        elif sc.final_status == "returned_to_hub":
            glat, glon = "", ""
        else:
            dist = sc.gps_far_m + rng.uniform(0, 120) if sc.gps_far_m else rng.uniform(5, 60)
            glat, glon = offset(lat, lon, dist, rng.uniform(0, 360))
        loc = f"{locality} Hub" if sc.final_status == "returned_to_hub" else locality
        rows.append([final_time, sc.final_status, loc, glat, glon])

    with open(case / "tracking.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["timestamp", "status", "location", "lat", "lon"])
        for r in rows:
            wr.writerow([r[0].strftime("%Y-%m-%d %H:%M:%S"), *r[1:]])

    meta: dict = {
        "shipment_id": sid, "tracking_id": tid, "customer_first_name": first,
        "address": f"House {gate_no}, {locality}, {CITY}", "gate_number": gate_no,
        "address_lat": lat, "address_lon": lon, "promised_by": promised.strftime("%Y-%m-%d %H:%M:%S"),
        "driver": driver, "synthetic": True,
    }

    ref = final_time or ofd
    if sc.photo is not None:
        p = sc.photo
        gate_text = {"match": gate_no, "other": str(int(gate_no) + rng.choice([-7, 3, 12, 41])), None: None}[p["gate"]]
        img = draw_pod(rng, gate_text, p["door"], p["package"], p["damage"])
        (case / "pod.jpg").write_bytes(img)
        meta["photo_taken_at"] = (ref + timedelta(minutes=rng.randint(0, 3))).strftime("%Y-%m-%d %H:%M:%S")
        scene = ("parcel at a gate" if p["package"] else "closed gate, no parcel") if p["door"] else "parcel on a ledge, no door in view"
        seed_cache("vision", hashlib.sha256(img).hexdigest(), VisionResult(
            door_visible=p["door"], package_visible=p["package"], gate_number=gate_text,
            damage_visible=p["damage"], scene=scene + (", box crushed" if p["damage"] else ""), confidence=0.9))

    if sc.note:
        (case / "driver_note.txt").write_text(sc.note, encoding="utf-8")
        meta["driver_note_recorded_at"] = (ref + timedelta(minutes=rng.randint(1, 4))).strftime("%Y-%m-%d %H:%M:%S")
        meta["transcript_is_cached"] = True
        if audio:
            try:
                from gtts import gTTS

                gTTS(sc.note, lang="en", tld="co.in").save(str(case / "driver_note.mp3"))
                mp3 = (case / "driver_note.mp3").read_bytes()
                cache = ROOT / "data" / "llm_cache" / f"whisper_{hashlib.sha256(mp3).hexdigest()[:24]}.json"
                cache.write_text(Transcript(text=sc.note).model_dump_json(), encoding="utf-8")
            except Exception as e:  # offline: keep the transcript only
                print(f"  (audio skipped for {sid}: {e})")

    if sc.customer:
        ct = ref + timedelta(hours=rng.uniform(1, 6))
        lines = []
        for msg in sc.customer:
            lines.append(f"[{ct:%Y-%m-%d %H:%M}] {msg}")
            ct += timedelta(minutes=rng.randint(5, 50))
        (case / "customer.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (case / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"case_id": sid, "cause": sc.cause, "contradictions": ";".join(sorted(sc.contradictions)),
            "gps_mismatch": int("gps_vs_address" in sc.contradictions)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--test-count", type=int, default=10)
    ap.add_argument("--seed", type=int, default=27)
    ap.add_argument("--audio", action="store_true", help="also synthesise driver_note.mp3 via gTTS (internet)")
    args = ap.parse_args()

    labels = []
    for split, out, n, seed, start in [
        ("dev", ROOT / "data" / "cases", args.count, args.seed, 1001),
        ("test", ROOT / "data" / "test_cases", args.test_count, args.seed + 9999, 9001),
    ]:
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        rng = random.Random(seed)
        fake = Faker("en_IN")
        fake.seed_instance(seed)
        base = scenarios()
        order = list(range(len(base)))
        if split == "test":
            rng.shuffle(order)
        for i in range(n):
            sc = base[order[i % len(base)]]
            row = write_case(out, start + i, sc, rng, fake, args.audio)
            labels.append({"split": split, **row})
        print(f"{split}: {n} cases -> {out.relative_to(ROOT)}")

    with open(ROOT / "data" / "labels.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=["split", "case_id", "cause", "contradictions", "gps_mismatch"])
        wr.writeheader()
        wr.writerows(labels)
    print("labels -> data/labels.csv")


if __name__ == "__main__":
    main()
