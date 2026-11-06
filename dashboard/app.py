"""RouteException dispatcher dashboard (Streamlit). Talks to the FastAPI backend only.

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="RouteException · Dispatcher", page_icon="📦", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 1.4rem; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .p-processing { background:#e8eefc; color:#1d4ed8; } .p-needs_review { background:#fff4e0; color:#9a5b00; }
  .p-approved { background:#e6f6ee; color:#0d7a4f; } .p-sent { background:#e9e9ef; color:#3f3f55; }
  .p-failed { background:#fde8e8; color:#b42318; }
  .sev-high { border-left:4px solid #d92d20; } .sev-medium { border-left:4px solid #f79009; } .sev-low { border-left:4px solid #98a2b3; }
  .contra { padding:8px 12px; margin:6px 0; background:rgba(127,127,127,.07); border-radius:6px; }
  .src { font-family:ui-monospace,monospace; font-size:12px; opacity:.75; }
  .card { border:1px solid rgba(127,127,127,.25); border-radius:10px; padding:14px 16px; }
</style>
""", unsafe_allow_html=True)

CAUSE_LABEL = {
    "address_issue": "📍 Address issue", "customer_unavailable": "🚪 Customer unavailable",
    "damaged": "📦 Damaged", "wrong_delivery": "⚠️ Wrong delivery", "delayed": "⏱️ Delayed",
    "needs_review": "🔍 Needs review",
}
RULE_LABEL = {
    "status_vs_customer": "Status vs customer", "photo_vs_address": "Photo vs address",
    "status_vs_driver_note": "Status vs driver note", "time_gap": "Time gap",
    "missing_evidence": "Missing evidence", "gps_vs_address": "GPS vs address",
}


# ------------------------------------------------------------------ API helpers
def api(method: str, path: str, **kw):
    try:
        r = requests.request(method, f"{API}{path}", timeout=60, **kw)
    except requests.ConnectionError:
        st.error(f"Cannot reach the API at {API}. Start it with `uvicorn app.main:app --reload`.")
        st.stop()
    return r


def pill(status: str) -> str:
    return f'<span class="pill p-{status}">{status.replace("_", " ")}</span>'


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("📦 RouteException")
    st.caption("Dispatcher console")
    h = api("GET", "/health").json()
    st.write(f"LLM: **{h['llm_provider']}**" + (" · live" if h["llm_live"] else " · cached/offline"))
    page = st.radio("View", ["Exception queue", "New shipment", "Evaluation", "Audit log"], label_visibility="collapsed")
    st.divider()
    if st.button("Load demo cases", width="stretch"):
        r = api("POST", "/shipments/import-demo")
        st.toast(f"Imported {len(r.json().get('imported', []))} cases" if r.ok else r.text)
    st.link_button("Driver app ↗", f"{API}/driver", width="stretch")
    st.link_button("API docs ↗", f"{API}/docs", width="stretch")


# ------------------------------------------------------------------ pages
def queue_page() -> None:
    rows = api("GET", "/shipments").json()
    if not rows:
        st.info("No shipments yet. Click **Load demo cases** in the sidebar, or add one under **New shipment**.")
        return

    df = pd.DataFrame(rows)
    c = st.columns(5)
    for col, (label, n) in zip(c, [
        ("Needs review", (df.status == "needs_review").sum()), ("Processing", (df.status == "processing").sum()),
        ("Approved", (df.status == "approved").sum()), ("Sent", (df.status == "sent").sum()),
        ("With contradictions", (df.contradiction_count > 0).sum()),
    ]):
        col.metric(label, int(n))

    left, right = st.columns([0.36, 0.64], gap="large")
    with left:
        f = st.segmented_control("Filter", ["needs_review", "processing", "approved", "sent", "all"],
                                 default="needs_review", format_func=lambda s: s.replace("_", " "))
        view = df if f in (None, "all") else df[df.status == f]
        if view.empty:
            st.caption("Nothing here.")
            return
        show = view.assign(
            cause=view.cause.map(lambda c: CAUSE_LABEL.get(c, "—") if c else "—"),
            conf=view.confidence.map(lambda x: f"{x:.0%}" if pd.notna(x) else ""),
        )[["id", "status", "cause", "conf", "contradiction_count"]]
        ev = st.dataframe(show.rename(columns={"contradiction_count": "⚠"}), hide_index=True,
                          width="stretch", on_select="rerun", selection_mode="single-row", height=520)
        sel_rows = ev.selection.rows if ev and ev.selection else []
        sid = view.iloc[sel_rows[0]]["id"] if sel_rows else st.session_state.get("sid", view.iloc[0]["id"])
        if sid not in set(view.id):
            sid = view.iloc[0]["id"]
        st.session_state.sid = sid
    with right:
        detail_panel(sid)


def detail_panel(sid: str) -> None:
    d = api("GET", f"/shipments/{sid}").json()
    a = d.get("analysis")
    st.markdown(f"### {sid} &nbsp; {pill(d['status'])}", unsafe_allow_html=True)
    st.caption(f"{d['tracking_id']} · {d['address']}")
    if d["status"] == "processing" or not a:
        st.info("Pipeline running… refresh in a moment.")
        if st.button("Refresh"):
            st.rerun()
        return
    if d["status"] == "failed":
        st.error(d["error"])
        return

    card, cause, act = a["task_card"], a["cause"], a["next_action"]
    t1, t2, t3, t4, t5 = st.tabs(["Task card", "Timeline", "Evidence", "Customer update", "Audit"])

    with t1:
        m = st.columns(4)
        m[0].metric("Cause", CAUSE_LABEL.get(cause["cause"], cause["cause"]).split(" ", 1)[-1])
        m[1].metric("Confidence", f"{cause['confidence']:.0%}")
        m[2].metric("Priority", act["priority"])
        m[3].metric("Escalate in", f"{act['escalate_after_hours']} h")
        if cause["needs_review"]:
            st.warning("Low confidence — confirm the cause in **Customer update** before approving.")
        st.markdown(f"**{card['title']}**  \n{card['summary']}")
        st.markdown(f"**Next action** (from `rules.yaml`, owner: `{act['owner']}`)")
        for item in card["checklist"]:
            st.checkbox(item, key=f"chk-{sid}-{item}")
        st.caption(f"Classifier engine: {cause['engine']} · triggered by {', '.join(act['triggered_by'])}")

        st.markdown(f"**Contradictions ({len(a['contradictions'])})**")
        if not a["contradictions"]:
            st.success("No contradictions between sources.")
        for c in a["contradictions"]:
            srcs = " · ".join(f"{s['file']} @ {s['locator']}" for s in c["evidence"])
            st.markdown(
                f'<div class="contra sev-{c["severity"]}"><b>{RULE_LABEL.get(c["rule"], c["rule"])}</b> '
                f'<span class="pill p-{"failed" if c["severity"] == "high" else "needs_review"}">{c["severity"]}</span>'
                f'<br>{c["summary"]}<br><span class="src">{srcs}</span></div>', unsafe_allow_html=True)

        if cause["evidence_ids"]:
            st.markdown("**Evidence cited for the cause**")
            by_id = {e["id"]: e for e in a["timeline"]}
            for i in cause["evidence_ids"]:
                e = by_id.get(i)
                if e:
                    st.markdown(f"- `{i}` {e['description']}  <span class='src'>{e['source']['file']} @ "
                                f"{e['source']['locator']}</span>", unsafe_allow_html=True)

    with t2:
        icon = {"scan": "🛰️", "photo": "📷", "driver_note": "🎙️", "customer_message": "💬"}
        tl = pd.DataFrame([{
            "id": e["id"], "time": (e["time"] or "")[:16].replace("T", " "), "": icon[e["kind"]],
            "actor": e["actor"], "event": e["description"], "source": f"{e['source']['file']} @ {e['source']['locator']}",
        } for e in a["timeline"]])
        st.dataframe(tl, hide_index=True, width="stretch")

    with t3:
        ex = a["extraction"]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Proof-of-delivery photo**")
            if ex["photo_file"]:
                img = api("GET", f"/shipments/{sid}/files/{ex['photo_file']}")
                if img.ok:
                    st.image(img.content, width="stretch")
                st.caption(f"Vision engine: {ex['vision_engine']}")
                if ex["vision"]:
                    st.json(ex["vision"], expanded=False)
                elif ex["vision_engine"] == "unavailable":
                    st.info("Photo not analysed — configure an LLM provider in .env for live vision.")
            else:
                st.warning("No photo on file.")
        with c2:
            st.markdown("**Delivery GPS vs address**")
            meta = ex["meta"]
            pts = [{"lat": meta["address_lat"], "lon": meta["address_lon"], "what": "address", "color": "#1f5eff"}]
            final = [r for r in ex["tracking"] if r["status"] in ("delivered", "attempted") and r["lat"] is not None]
            if final:
                pts.append({"lat": final[-1]["lat"], "lon": final[-1]["lon"], "what": final[-1]["status"], "color": "#d92d20"})
            st.map(pd.DataFrame(pts), latitude="lat", longitude="lon", color="color", size=40, zoom=14)
            st.caption("🔵 registered address · 🔴 driver scan (dispatcher-only; never shown to customers)")
        st.markdown("**Driver voice note**")
        if ex["transcript"]:
            if ex["note_file"] and not ex["note_file"].endswith(".txt"):
                au = api("GET", f"/shipments/{sid}/files/{ex['note_file']}")
                if au.ok:
                    st.audio(au.content)
            st.markdown(f"> {ex['transcript']['text']}")
            st.caption(f"Transcript engine: {ex['transcript_engine']} · claims: "
                       f"{', '.join(c['type'] for c in ex['driver_claims']) or 'none'}")
        else:
            st.caption("No voice note.")
        st.markdown("**Customer messages**")
        for c in ex["customer_claims"]:
            st.markdown(f"- `{c['type']}` “{c['text']}” <span class='src'>{c['source']['file']} @ "
                        f"{c['source']['locator']}</span>", unsafe_allow_html=True)
        if not ex["customer_claims"]:
            st.caption("No customer messages.")
        st.markdown("**Tracking scans**")
        st.dataframe(pd.DataFrame(ex["tracking"]), hide_index=True, width="stretch")

    with t4:
        customer_update(sid, d, a)

    with t5:
        rows = api("GET", f"/shipments/{sid}/audit").json()
        for r in reversed(rows):
            with st.expander(f"{r['created_at'][:19].replace('T', ' ')} · {r['actor']} · {r['action']}"):
                st.json(r["details"])


def customer_update(sid: str, d: dict, a: dict) -> None:
    cause = a["cause"]
    if d["status"] == "needs_review":
        st.markdown("**1 · Confirm cause**")
        opts = list(CAUSE_LABEL)[:-1]
        current = cause["cause"] if cause["cause"] in opts else (cause.get("candidate") or opts[0])
        cc = st.columns([3, 1])
        new = cc[0].selectbox("Cause", opts, index=opts.index(current), format_func=CAUSE_LABEL.get,
                              key=f"cause-{sid}", label_visibility="collapsed")
        if cc[1].button("Confirm", key=f"setc-{sid}", width="stretch",
                        disabled=(new == cause["cause"] and not cause["needs_review"])):
            r = api("POST", f"/shipments/{sid}/cause", json={"cause": new})
            st.toast("Cause updated, next action re-looked-up" if r.ok else r.text)
            st.rerun()

    draft = next((m for m in d["messages"] if m["status"] == "draft"), None)
    history = [m for m in d["messages"] if m["status"] in ("approved", "sent")]

    if draft:
        st.markdown("**2 · Review the customer message**")
        st.caption(f"Drafted by: {a['draft_engine']} · every edit is guardrail-checked and logged")
        text = st.text_area("Message", value=draft["final_text"] or draft["draft_text"], height=150,
                            key=f"txt-{sid}-{draft['id']}", label_visibility="collapsed")
        g = api("POST", "/shipments/check-text", json={"text": text}).json()
        if g["ok"]:
            st.success("Guardrail: no refund promises, phone numbers, blame or GPS found.")
        else:
            st.error("Guardrail blocked: " + ", ".join(f"{v['type']} “{v['match']}”" for v in g["violations"]))
        b = st.columns(2)
        if b[0].button("Save edit", key=f"save-{sid}", width="stretch"):
            r = api("PUT", f"/shipments/{sid}/message", json={"text": text}).json()
            if r["unsupported_facts"]:
                st.warning(f"Unsupported facts (not in evidence): {r['unsupported_facts']}")
            st.toast("Saved (logged to audit)")
        disabled = (not g["ok"]) or cause["cause"] == "needs_review"
        if b[1].button("✅ Approve", key=f"appr-{sid}", type="primary", width="stretch", disabled=disabled):
            r = api("POST", f"/shipments/{sid}/approve", json={"text": text, "approver": "dispatcher"})
            if r.ok:
                st.toast("Approved — audio generated" if any(m["has_audio"] for m in r.json()["messages"])
                         else "Approved (audio unavailable offline)")
                st.rerun()
            else:
                st.error(r.json().get("detail"))
        if cause["cause"] == "needs_review":
            st.caption("Confirm a cause first.")

    for m in history:
        st.markdown(f"**{m['status'].title()}** by {m['approved_by']} · {(m['approved_at'] or '')[:16].replace('T', ' ')}")
        st.info(m["final_text"])
        if m["has_audio"]:
            au = api("GET", f"/shipments/{sid}/audio/{m['id']}")
            if au.ok:
                st.audio(au.content, format="audio/mpeg")
    if d["status"] == "approved":
        if st.button("📤 Send to customer", type="primary", key=f"send-{sid}"):
            r = api("POST", f"/shipments/{sid}/send")
            st.toast("Sent" if r.ok else r.text)
            st.rerun()
    if d["status"] in ("approved", "sent"):
        st.link_button("Open customer tracking page ↗", f"{API}/track/{d['tracking_id']}")


def new_shipment_page() -> None:
    st.subheader("New shipment")
    st.caption("Intake saves the files, returns immediately, and runs the pipeline in the background.")
    with st.form("new"):
        c = st.columns(3)
        sid = c[0].text_input("Shipment ID", "SHP-2001")
        tid = c[1].text_input("Tracking ID", "RX20001")
        first = c[2].text_input("Customer first name", "Asha")
        addr = st.text_input("Address", "House 214, Lotus Vihar, Navpur")
        c = st.columns(4)
        gate = c[0].text_input("Gate number", "214")
        lat = c[1].number_input("Address lat", value=30.7046, format="%.6f")
        lon = c[2].number_input("Address lon", value=76.7179, format="%.6f")
        promised = c[3].text_input("Promised by", "2026-11-05 18:00:00")
        tracking = st.file_uploader("tracking.csv (timestamp,status,location,lat,lon)", type=["csv"])
        photo = st.file_uploader("Delivery photo", type=["jpg", "jpeg", "png"])
        note = st.file_uploader("Driver voice note", type=["mp3", "wav", "m4a", "webm", "ogg"])
        note_text = st.text_area("…or typed driver note")
        cust = st.file_uploader("customer.txt (one message per line, optional [YYYY-MM-DD HH:MM] prefix)", type=["txt"])
        go = st.form_submit_button("Submit", type="primary")
    if go:
        if not tracking:
            st.error("tracking.csv is required")
            return
        meta = {"shipment_id": sid, "tracking_id": tid, "customer_first_name": first, "address": addr,
                "gate_number": gate, "address_lat": lat, "address_lon": lon, "promised_by": promised, "driver": {}}
        files = {"meta_json": ("meta.json", json.dumps(meta)), "tracking_csv": ("tracking.csv", tracking.getvalue())}
        if photo:
            files["photo"] = (photo.name, photo.getvalue())
        if note:
            files["driver_note"] = (note.name, note.getvalue())
        if cust:
            files["customer_txt"] = ("customer.txt", cust.getvalue())
        r = api("POST", "/shipments", files=files, data={"driver_note_text": note_text} if note_text else {})
        if r.ok:
            st.success(f"{sid} received — status `{r.json()['status']}`. Open it in the exception queue.")
            st.session_state.sid = sid
        else:
            st.error(r.text)


def evaluation_page() -> None:
    st.subheader("Evaluation")
    st.caption("Runs the full pipeline on a labelled folder. The hidden test set is never used for tuning.")
    split = st.radio("Case set", ["data/test_cases", "data/cases"], horizontal=True)
    if st.button("Run evaluation", type="primary"):
        sys.path.insert(0, str(ROOT))
        from scripts.evaluate import TARGETS, evaluate, passes

        with st.spinner("Running pipeline on every case…"):
            res = evaluate(ROOT / split, ROOT / "data" / "labels.csv")
        m = res["metrics"]
        cols = st.columns(4)
        for i, (k, (op, t)) in enumerate(TARGETS.items()):
            v = m[k]
            shown = f"{v:.0%}" if isinstance(v, float) else str(v)
            cols[i % 4].metric(k.replace("_", " ").capitalize(), shown,
                               delta=("PASS" if passes(k, v) else "FAIL") + f" (target {op}{t:.0%})" if isinstance(t, float)
                               else ("PASS" if passes(k, v) else "FAIL") + f" (target {op}{t})",
                               delta_color="normal" if passes(k, v) else "inverse")
        st.dataframe(pd.DataFrame(res["per_case"]), hide_index=True, width="stretch")


def audit_page() -> None:
    st.subheader("Audit log")
    rows = api("GET", "/audit").json()
    if rows:
        df = pd.DataFrame(rows)
        df["details"] = df.details.map(lambda x: json.dumps(x)[:160])
        st.dataframe(df, hide_index=True, width="stretch")
    else:
        st.caption("Empty.")


{"Exception queue": queue_page, "New shipment": new_shipment_page,
 "Evaluation": evaluation_page, "Audit log": audit_page}[page]()
