"""Fraud-report evidence pack (PDF) for the customer.

Contains only customer-safe data: no driver phone/name, no GPS. We never contact the police;
the pack tells the customer how to file the complaint themselves (1930 / cybercrime.gov.in).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REQUIRED_SECTIONS = (
    "Shipment details",
    "Delivery timeline",
    "Proof-of-delivery photo",
    "Updates we have sent you",
    "Your report",
    "How to file a complaint",
)


def build_evidence_pack(
    out_path: Path,
    *,
    shipment: dict[str, Any],
    timeline: list[dict[str, Any]],
    photo_path: Path | None,
    sent_messages: list[dict[str, Any]],
    report_text: str,
    fraud_cfg: dict[str, str],
) -> list[str]:
    """Write the PDF; return the list of sections included (for completeness checks)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ss = getSampleStyleSheet()
    h, p = ss["Heading2"], ss["BodyText"]
    story: list[Any] = []
    included: list[str] = []

    def section(title: str) -> None:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(title, h))
        included.append(title)

    story.append(Paragraph("RouteException · Delivery evidence pack", ss["Title"]))
    story.append(Paragraph(
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} for the customer's own use.", p))

    section("Shipment details")
    story.append(Table(
        [["Tracking ID", shipment["tracking_id"]], ["Shipment", shipment["id"]],
         ["Delivery address", shipment["address"]]],
        colWidths=[40 * mm, 130 * mm],
        style=TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                          ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke)]),
    ))

    section("Delivery timeline")
    rows = [["Time", "Event", "Location"]]
    for e in timeline:
        rows.append([e["time"], Paragraph(escape(e["status"]), p), Paragraph(escape(e["location"]), p)])
    story.append(Table(rows, colWidths=[38 * mm, 52 * mm, 80 * mm], repeatRows=1, style=TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])))

    section("Proof-of-delivery photo")
    if photo_path and photo_path.exists():
        story.append(Image(str(photo_path), width=90 * mm, height=67 * mm, kind="proportional"))
    else:
        story.append(Paragraph("No proof-of-delivery photo is on record for this shipment.", p))

    section("Updates we have sent you")
    if sent_messages:
        for m in sent_messages:
            story.append(Paragraph(f"<b>{m['sent_at']}</b> — {escape(m['text'])}", p))
    else:
        story.append(Paragraph("No updates have been sent yet.", p))

    section("Your report")
    story.append(Paragraph(escape(report_text), p))

    section("How to file a complaint")
    story.append(Paragraph(
        "RouteException does not contact the police or accuse anyone on your behalf. "
        "If you believe this is fraud, you can file a complaint yourself:", p))
    story.append(Paragraph(f"• Call the National Cyber Crime Helpline: <b>{fraud_cfg['helpline']}</b>", p))
    story.append(Paragraph(f"• File online at <b>{fraud_cfg['portal']}</b>", p))
    story.append(Paragraph("• Attach this PDF to your complaint.", p))

    SimpleDocTemplate(str(out_path), pagesize=A4, title="Delivery evidence pack",
                      leftMargin=18 * mm, rightMargin=18 * mm).build(story)
    return included


def is_complete(sections: list[str]) -> bool:
    return all(s in sections for s in REQUIRED_SECTIONS)
