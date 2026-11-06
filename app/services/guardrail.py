"""Blocks unsafe outgoing text. Code only. Every message type must pass through check_text()."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.schemas import GuardrailResult

# Refund / compensation promises.
_REFUND = re.compile(
    r"\b(refund(s|ed|ing)?|compensat(e|ed|ion|ing)|reimburs(e|ed|ement)|money[- ]back|"
    r"cash[- ]?back|free of (charge|cost)|voucher|coupon|credit (note|to your account)|"
    r"we will pay|pay you back)\b",
    re.I,
)

# Phone numbers: Indian mobiles (+91 optional), generic 10+ digit runs, grouped numbers.
_PHONE = re.compile(
    r"(\+?\d[\d\s\-().]{8,}\d)|(\b[6-9]\d{9}\b)",
)

# Blame phrases aimed at the customer or the driver.
_BLAME = re.compile(
    r"\b(your fault|you (were|are) (not|never) (home|there|available)|you failed|you didn'?t|"
    r"you did not|you forgot|you gave (a |the )?wrong|because of you|customer'?s fault|"
    r"customer (is|was) (lying|responsible|at fault)|driver'?s fault|driver (is|was) "
    r"(lying|negligent|careless|responsible|at fault)|(the |our )?driver (stole|lied|lost)|"
    r"blame|negligen(t|ce)|lying|liar|fraudster|scam(mer)?|theft by|stole)\b",
    re.I,
)

# GPS coordinates (driver location must never reach the customer).
_COORDS = re.compile(r"-?\d{1,2}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# Automatic accusations / police contact.
_POLICE = re.compile(r"\b(we (have|will) (reported|report|contact(ed)?|inform(ed)?) (you to )?(the )?police|FIR (has been|was) filed)\b", re.I)


def check_text(text: str, private_terms: Iterable[str] = ()) -> GuardrailResult:
    """Return every violation. `private_terms` = driver name/phone etc. that must never appear."""
    violations: list[dict[str, str]] = []

    def add(kind: str, match: str) -> None:
        violations.append({"type": kind, "match": match.strip()})

    for m in _REFUND.finditer(text):
        add("refund_or_compensation", m.group(0))
    for m in _PHONE.finditer(text):
        if sum(ch.isdigit() for ch in m.group(0)) >= 10:
            add("phone_number", m.group(0))
    for m in _BLAME.finditer(text):
        add("blame_phrase", m.group(0))
    for m in _COORDS.finditer(text):
        add("gps_coordinates", m.group(0))
    for m in _EMAIL.finditer(text):
        add("email_address", m.group(0))
    for m in _POLICE.finditer(text):
        add("police_accusation", m.group(0))

    for term in private_terms:
        t = (term or "").strip()
        if len(t) < 3:
            continue
        # Full names / phones / IDs: case-insensitive. Single name tokens: case-sensitive, so a
        # surname like "Date" does not block the ordinary word "date".
        flags = re.I if (" " in t or any(ch.isdigit() for ch in t)) else 0
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text, flags):
            add("private_detail", t)

    return GuardrailResult(ok=not violations, violations=violations)
