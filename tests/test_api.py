"""End-to-end: the MVP demo. A new shipment with a status/photo contradiction must be flagged and the
customer message must stay neutral; nothing reaches the customer before approval."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "cases" / "SHP-1001"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def new_shipment(client):
    meta = json.loads((DEMO / "meta.json").read_text())
    meta.update(shipment_id="SHP-7001", tracking_id="RX77001")
    files = {
        "meta_json": ("meta.json", json.dumps(meta)),
        "tracking_csv": ("tracking.csv", (DEMO / "tracking.csv").read_bytes()),
        "photo": ("pod.jpg", (DEMO / "pod.jpg").read_bytes()),
        "customer_txt": ("customer.txt", (DEMO / "customer.txt").read_bytes()),
    }
    r = client.post("/shipments", files=files,
                    data={"driver_note_text": (DEMO / "driver_note.txt").read_text()})
    assert r.status_code == 202 and r.json()["status"] == "processing"
    return meta


def test_new_shipment_flags_status_photo_contradiction(client, new_shipment):
    d = client.get("/shipments/SHP-7001").json()
    assert d["status"] == "needs_review"
    rules = {c["rule"] for c in d["analysis"]["contradictions"]}
    assert {"status_vs_customer", "photo_vs_address"} <= rules
    assert d["analysis"]["guardrail"]["ok"]
    draft = d["messages"][-1]["draft_text"]
    driver = new_shipment["driver"]
    assert driver["name"] not in draft and driver["phone"] not in draft


def test_customer_sees_nothing_before_approval(client, new_shipment):
    assert client.get("/api/customer/RX77001").json()["updates"] == []


def test_guardrail_blocks_unsafe_approval(client, new_shipment):
    r = client.post("/shipments/SHP-7001/approve",
                    json={"text": "Sorry, the driver's fault. We will refund you. Call 9876543210."})
    assert r.status_code == 422
    kinds = {v["type"] for v in r.json()["detail"]["violations"]}
    assert {"blame_phrase", "refund_or_compensation", "phone_number"} <= kinds
    assert client.get("/shipments/SHP-7001").json()["status"] == "needs_review"


def test_approve_send_and_customer_view(client, new_shipment):
    r = client.post("/shipments/SHP-7001/approve", json={"approver": "test-dispatcher"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.get("/api/customer/RX77001").json()["updates"] == []  # approved but not sent yet
    assert client.post("/shipments/SHP-7001/send").json()["status"] == "sent"

    body = client.get("/api/customer/RX77001").text
    assert "updates" in body and len(json.loads(body)["updates"]) == 1
    driver = new_shipment["driver"]
    for secret in (driver["name"], driver["phone"], str(new_shipment["address_lat"])):
        assert secret not in body


def test_fraud_report_builds_pack_and_contacts_nobody(client, new_shipment):
    r = client.post("/api/customer/RX77001/fraud-report",
                    json={"description": "Marked delivered but I never got it. Someone else signed."})
    assert r.status_code == 200
    d = r.json()
    assert d["helpline"] == "1930" and "cybercrime.gov.in" in d["portal"]
    pdf = client.get(d["evidence_pack_url"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_audit_log_records_everything(client, new_shipment):
    actions = [a["action"] for a in client.get("/shipments/SHP-7001/audit").json()]
    for a in ("shipment_received", "pipeline_completed", "approval_blocked", "message_approved",
              "message_sent", "fraud_report"):
        assert a in actions


def test_driver_live_update_reprocesses(client):
    client.post("/shipments/import-demo")
    stops = client.get("/api/driver/stops").json()
    assert all("phone" not in json.dumps(s) for s in stops)
    meta = json.loads((DEMO / "meta.json").read_text())
    far_lat = meta["address_lat"] + 0.01  # ~1.1 km away
    r = client.post("/api/driver/stops/SHP-1002/update",
                    data={"status": "delivered", "lat": far_lat, "lon": meta["address_lon"],
                          "note_text": "Handed it to the guard."})
    assert r.status_code == 202
    d = client.get("/shipments/SHP-1002").json()
    assert "gps_vs_address" in {c["rule"] for c in d["analysis"]["contradictions"]}


def test_dispatcher_cause_override_relooks_up_action(client):
    client.post("/shipments/import-demo")
    d = client.get("/shipments/SHP-1004").json()
    assert d["status"] == "needs_review"
    r = client.post("/shipments/SHP-1004/cause", json={"cause": "delayed"})
    assert r.status_code == 200 and r.json()["analysis"]["cause"]["engine"] == "dispatcher"
    assert r.json()["analysis"]["next_action"]["action"].startswith("Re-prioritise")
