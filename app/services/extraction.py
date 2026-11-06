"""Extraction (AI layer): Whisper transcript, vision JSON from photo, claims from free text.

Every extracted field keeps its source (file + row/line/'image'/'transcript').
The AI only labels; quoted text is always copied verbatim from the source by code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel

from app.config import get_settings
from app.schemas import Claim, Extraction, Source, TrackingRow, Transcript, VisionResult
from app.services.llm import call_json

log = logging.getLogger("routeexception.extraction")

PHOTO_NAMES = ("pod.jpg", "pod.jpeg", "pod.png")
AUDIO_NAMES = ("driver_note.mp3", "driver_note.wav", "driver_note.m4a", "driver_note.webm", "driver_note.ogg")

CustomerClaimType = Literal[
    "not_received", "damaged", "wrong_item", "late", "customer_was_home", "address_problem", "other"
]
DriverClaimType = Literal[
    "delivered", "customer_absent", "address_not_found", "damaged", "not_delivered", "returned", "other"
]

# ------------------------------------------------------------------ keyword fallbacks (deterministic)
_CUSTOMER_PATTERNS: list[tuple[str, str]] = [
    ("damaged", r"damag|broken|crushed|torn|leak|smashed|dented|wet box"),
    ("wrong_item", r"wrong (item|product|parcel|package)|not what i ordered|someone else'?s"),
    ("customer_was_home", r"(i was|we were|i am|someone was) (at )?home|was home all|nobody (came|rang|knocked)|no one (came|rang|knocked)|did(n'?t| not) (ring|knock|call)"),
    ("not_received", r"(never|not|haven'?t|didn'?t|did not) (got|get|received?|arrived?|deliver)|no parcel|not delivered|where is my|missing|marked (as )?delivered"),
    ("address_problem", r"address|landmark|wrong (house|flat|building|gate)|couldn'?t find"),
    ("late", r"late|delay|still waiting|days? (ago|overdue)|promised|taking (so )?long|when will"),
]
_DRIVER_PATTERNS: list[tuple[str, str]] = [
    ("damaged", r"damag|broken|crushed|torn|leak|dented"),
    ("address_not_found", r"(couldn'?t|could not|can'?t|cannot|unable to) (find|locate)|address (was )?(wrong|incomplete|not found|unclear)|no such (house|gate|address)"),
    ("customer_absent", r"nobody|no one|no-one|not (at )?home|didn'?t (answer|pick)|not answering|locked|customer (was )?(not available|unavailable|absent)"),
    ("returned", r"return(ed|ing)? (to|it to) (the )?hub|taking it back|bring(ing)? it back"),
    ("not_delivered", r"(not|couldn'?t|could not|didn'?t) deliver"),
    ("delivered", r"(handed|delivered|left|gave|placed) (it |the parcel |the package )?(to|at|with|over|near|by|in)|delivered successfully|signed for"),
]


def _match_type(text: str, patterns: list[tuple[str, str]]) -> str:
    low = text.lower()
    for t, pat in patterns:
        if re.search(pat, low):
            return t
    return "other"


# ------------------------------------------------------------------ tracking
def parse_tracking(path: Path) -> list[TrackingRow]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    rows: list[TrackingRow] = []
    for i, rec in enumerate(df.to_dict("records"), start=1):
        lat = rec.get("lat")
        lon = rec.get("lon")
        rows.append(
            TrackingRow(
                row=i,
                timestamp=pd.to_datetime(rec["timestamp"]).to_pydatetime(),
                status=str(rec["status"]).strip().lower(),
                location=str(rec.get("location") or "").strip() if pd.notna(rec.get("location")) else "",
                lat=float(lat) if pd.notna(lat) else None,
                lon=float(lon) if pd.notna(lon) else None,
            )
        )
    return rows


# ------------------------------------------------------------------ vision
_VISION_PROMPT = """You are inspecting a proof-of-delivery photo for a parcel delivery.
Describe ONLY what is visible. Do not guess.
- door_visible: is a door or gate clearly visible?
- package_visible: is a parcel/package visible?
- gate_number: the house/gate number printed in the photo, exactly as written, or null if unreadable.
- damage_visible: does the parcel look damaged (crushed, torn, wet)?
- scene: under 15 words, neutral description.
- confidence: 0..1 how clearly you could read the photo."""


def analyse_photo(path: Path) -> tuple[VisionResult | None, str]:
    img = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    res = call_json(
        "vision", _VISION_PROMPT, VisionResult, image=img, image_mime=mime,
        cache_key=hashlib.sha256(img).hexdigest(),
    )
    if res is None:
        return None, "unavailable"
    return res


# ------------------------------------------------------------------ speech to text
_whisper_model = None


def transcribe(path: Path) -> tuple[Transcript | None, str]:
    audio = path.read_bytes()
    cache = get_settings().llm_cache_dir / f"whisper_{hashlib.sha256(audio).hexdigest()[:24]}.json"
    if cache.exists():
        return Transcript.model_validate_json(cache.read_text(encoding="utf-8")), "cache"
    try:
        import whisper  # type: ignore[import-not-found]
    except ImportError:
        log.warning("openai-whisper not installed; voice note %s not transcribed", path.name)
        return None, "unavailable"
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = whisper.load_model(get_settings().whisper_model)
    out = _whisper_model.transcribe(str(path), fp16=False)
    t = Transcript(text=out["text"].strip(), language=out.get("language", "en"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(t.model_dump_json(), encoding="utf-8")
    return t, "whisper"


# ------------------------------------------------------------------ claims
class _LineLabel(BaseModel):
    line: int
    type: str


class _Labels(BaseModel):
    labels: list[_LineLabel]


_CUSTOMER_LINE = re.compile(r"^\s*\[(?P<ts>[^\]]+)\]\s*(?P<text>.+)$")


def _label_lines(task: str, lines: list[str], allowed: tuple[str, ...], who: str,
                 patterns: list[tuple[str, str]]) -> tuple[list[str], str]:
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(lines, start=1))
    prompt = (
        f"Label each numbered line written by the {who} of a parcel delivery with exactly one type "
        f"from {list(allowed)}. Use 'other' if none fits. Never invent lines.\n\n{numbered}\n\n"
        'Return {"labels": [{"line": <n>, "type": "<type>"}]}'
    )
    res = call_json(task, prompt, _Labels)
    if res is not None:
        labels, engine = res
        by_line = {lab.line: lab.type for lab in labels.labels if lab.type in allowed}
        if len(by_line) == len(lines):
            return [by_line[i] for i in range(1, len(lines) + 1)], engine
    return [_match_type(t, patterns) for t in lines], "keywords"


def parse_customer(path: Path) -> tuple[list[Claim], str]:
    if not path.exists():
        return [], "none"
    raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    texts, times, line_nos = [], [], []
    for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not ln.strip():
            continue
        m = _CUSTOMER_LINE.match(ln)
        ts = None
        text = ln.strip()
        if m:
            text = m.group("text").strip()
            try:
                ts = pd.to_datetime(m.group("ts")).to_pydatetime()
            except (ValueError, TypeError):
                ts = None
        texts.append(text)
        times.append(ts)
        line_nos.append(n)
    if not raw_lines:
        return [], "none"
    types, engine = _label_lines(
        "customer_claims", texts, CustomerClaimType.__args__, "customer", _CUSTOMER_PATTERNS
    )
    claims = [
        Claim(type=t, text=txt, time=ts, source=Source(file=path.name, locator=f"line {n}"))
        for t, txt, ts, n in zip(types, texts, times, line_nos)
    ]
    return claims, engine


def driver_claims_from_transcript(t: Transcript, note_file: str, note_time: datetime | None) -> tuple[list[Claim], str]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t.text) if s.strip()]
    if not sentences:
        return [], "none"
    types, engine = _label_lines(
        "driver_claims", sentences, DriverClaimType.__args__, "driver", _DRIVER_PATTERNS
    )
    claims = [
        Claim(type=ty, text=s, time=note_time, source=Source(file=note_file, locator=f"sentence {i}"))
        for i, (ty, s) in enumerate(zip(types, sentences), start=1)
        if ty != "other"
    ]
    return claims, engine


# ------------------------------------------------------------------ main entry
def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return pd.to_datetime(value).to_pydatetime()


def extract_case(case_dir: Path) -> Extraction:
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    ex = Extraction(meta=meta, tracking=parse_tracking(case_dir / "tracking.csv"))

    photo = next((case_dir / n for n in PHOTO_NAMES if (case_dir / n).exists()), None)
    if photo:
        ex.photo_file = photo.name
        ex.photo_time = _dt(meta.get("photo_taken_at"))
        ex.vision, ex.vision_engine = analyse_photo(photo)

    audio = next((case_dir / n for n in AUDIO_NAMES if (case_dir / n).exists()), None)
    typed = case_dir / "driver_note.txt"
    if audio:
        ex.note_file = audio.name
        ex.transcript, ex.transcript_engine = transcribe(audio)
    if ex.transcript is None and typed.exists():
        # Typed note from the driver app, or the stored Whisper transcript for a demo case.
        ex.note_file = ex.note_file or typed.name
        ex.transcript = Transcript(text=typed.read_text(encoding="utf-8").strip())
        ex.transcript_engine = "cache" if meta.get("transcript_is_cached") else "typed"
    if ex.transcript:
        ex.note_time = _dt(meta.get("driver_note_recorded_at"))
        ex.driver_claims, _ = driver_claims_from_transcript(ex.transcript, ex.note_file or "", ex.note_time)

    ex.customer_claims, ex.claims_engine = parse_customer(case_dir / "customer.txt")
    return ex
