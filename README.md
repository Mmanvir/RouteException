# RouteException

Multimodal AI assistant for last-mile delivery exceptions (B.Tech CSE-AIML capstone, problem statement #27).

RouteException rebuilds a failed or disputed delivery from tracking scans, the delivery photo, the driver's
voice note and the customer's messages. It flags contradictions, picks a cause, looks up the next action and
drafts a neutral customer update. **A dispatcher approves every update before the customer sees it.**

> **Core principle:** code decides facts and rules. The AI only reads unstructured inputs (audio, images,
> free text) and drafts text. A human approves every outgoing message.

## Quick start (Windows, PowerShell)

```powershell
python -m venv venv; venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env                      # optional: add GEMINI_API_KEY / OPENAI_API_KEY
python scripts/generate_data.py --count 40  # synthetic cases + 10 hidden test cases

uvicorn app.main:app --reload               # terminal 1 -> http://localhost:8000
streamlit run dashboard/app.py              # terminal 2 -> http://localhost:8501
```

In the dashboard, click **Load demo cases**. `SHP-1001` is the MVP demo case. It is marked *delivered*,
the customer says it never arrived, the photo shows the wrong gate number and the GPS is about 900 m off.
All three contradictions are flagged with their sources, the priority is P1, and the draft stays neutral.

| Surface | URL |
|---|---|
| Dispatcher dashboard | http://localhost:8501 |
| Driver app (PWA) | http://localhost:8000/driver |
| Customer tracking | http://localhost:8000/track/RX10001 |
| API docs | http://localhost:8000/docs |

The demo runs **fully offline**. Set `LLM_PROVIDER=gemini` (or `openai`) plus a key in `.env` to use live
vision, claim labelling, cause classification and drafting.

## Demo script (5 minutes)

1. **Dashboard → Load demo cases.** The queue sorts by contradiction count.
2. **Open SHP-1001.** In *Task card*, three contradictions are shown with sources (`tracking.csv @ row 5`,
   `pod.jpg @ image`, `customer.txt @ line 1`). The next action comes from `rules.yaml` and is P1.
3. **Evidence tab.** It shows the POD photo with gate 677 against registered gate 674, and a map of the
   driver scan against the address (dispatcher only).
4. **Customer update tab.** Type *"Sorry, the driver's fault, we will refund you"*. The guardrail blocks it
   live and **Approve** is disabled. Restore the draft, then click **Approve**. Audio is generated now, not
   before. Then click **Send**.
5. **Customer page** `/track/RX10001`. It shows the approved update (with audio when online). Under
   **Report a problem**, a PDF evidence pack is created and the 1930 helpline and cybercrime.gov.in are
   shown. Nobody is contacted automatically.
6. **Driver app** `/driver` on a phone or a narrow browser. Pick a stop, mark it delivered, add a photo and
   GPS, and submit. The shipment goes back to `processing` and then to `needs_review` with new findings.
7. **Evaluation page.** Runs all metrics on the hidden test set.

## Architecture

```
intake ─► extraction (AI) ─► timeline (code) ─► rules (code) ─► cause (AI, fixed taxonomy)
        ─► next action (code, rules.yaml) ─► messages (AI/template) ─► guardrail (code)
        ─► dispatcher approval (human) ─► TTS ─► sent to customer
```

Shipment status: `processing → needs_review → approved → sent` (or `failed`, which stays visible).

| Step | File | How it stays safe |
|---|---|---|
| Extraction | `app/services/extraction.py` | The AI only *labels* lines from a fixed claim list. Quoted text is copied verbatim by code. Every claim keeps `file @ line/sentence/image`. |
| Timeline | `timeline.py` | Deterministic sort, every event tagged with its source, IDs `E1..En`. |
| Rules | `rules.py` | 6 rules: status vs customer, photo vs address, status vs driver note, time gap, missing evidence, GPS vs address (Haversine, over 200 m). |
| Cause | `classifier.py` | The LLM must choose one of 5 causes **and cite evidence IDs**. Unknown cause, invented IDs or low confidence lead to `needs_review`. Offline it uses a transparent weighted-evidence fallback. |
| Next action | `next_action.py` + `config/rules.yaml` | Pure lookup. Contradiction overrides can raise the priority. The LLM is never asked. |
| Messages | `messages.py` | LLM draft → guardrail + *unsupported-fact* check (every number, ID or date must come from the evidence). If it fails, the safe template from `rules.yaml` is used. |
| Guardrail | `guardrail.py` | Blocks refund/compensation wording, phone numbers, blame phrases, GPS coordinates, emails, police/FIR claims and the driver's name, phone and ID. Checked again at approval time, server-side. |
| LLM helper | `llm.py` | One function: JSON mode, Pydantic validation, one retry on invalid JSON, disk cache. |
| Evidence pack | `evidence_pack.py` | Customer-safe PDF (no GPS, no driver details) plus the 1930 helpline and cybercrime.gov.in. |
| Audit | `audit.py` | Every intake, pipeline run, edit, blocked approval, approval, send, cause override and fraud report is logged. |

## Evaluation

```powershell
python scripts/evaluate.py --cases data/test_cases
```

| Metric | Target | Hidden test set (offline mode) |
|---|---|---|
| Timeline completeness | ≥ 95% | 100% |
| Contradiction recall | ≥ 90% | 100% |
| Contradiction precision | ≥ 85% | 100% |
| Cause accuracy | ≥ 80% | 100% |
| Unsupported claims in customer messages | 0 | 0 |
| Guardrail violations in approved messages | 0 | 0 |
| GPS-mismatch detection | ≥ 90% | 100% |
| Evidence-pack completeness | 100% | 100% |

**Read these numbers honestly.** The data is synthetic and comes from the same generator. In offline mode,
the photo "vision" result is the generator's ground truth, seeded into `data/llm_cache/`, and claim labels
come from keyword rules. The scores show that the **code layer** (timeline, rules, next action, guardrail)
is correct. They do not show real-world AI accuracy. For a meaningful AI evaluation:

1. Replace the generated photos with team-shot door/gate photos (fake gate numbers) and record the voice notes.
2. Set `LLM_PROVIDER` and delete `data/llm_cache/vision_*.json` so vision runs live.
3. Install `openai-whisper` (and ffmpeg) for real transcription.
4. Re-run `evaluate.py` on `data/test_cases`. Never tune prompts or rules on it.

## Tests

```powershell
pytest          # 78 tests: every guardrail pattern, every rule, LLM retry/cache, full API flow
```

## Case folder format

```
data/cases/SHP-1001/
  meta.json        shipment_id, tracking_id, customer_first_name, address, gate_number,
                   address_lat/lon, promised_by, photo_taken_at, driver_note_recorded_at, driver{...}
  tracking.csv     timestamp,status,location,lat,lon
  pod.jpg          door/gate photo with printed (fake) gate number
  driver_note.mp3  optional voice note (driver_note.txt = transcript / typed note)
  customer.txt     one message per line, optional "[YYYY-MM-DD HH:MM] " prefix
```

## Notes and limits

- **Synthetic data only.** Names come from Faker, the city "Navpur" and its localities are fictional, and
  driver phones are fake `90000xxxxx` numbers that are never shown to customers.
- **Driver app on a real phone:** GPS and the microphone need HTTPS (browsers allow them only on
  `localhost`). Use a tunnel such as `ngrok http 8000`, or type the note instead.
- **Whisper** is optional. Without it, uploaded voice notes are stored and played for the dispatcher, and the
  typed note or stored transcript is used.
- **gTTS** needs internet. Offline, approved messages are text-only.
- **No authentication** in this prototype. Add dispatcher login and signed customer links before any real use.
- Next steps from the build plan: Hindi/Punjabi audio, batch view, escalation deadlines, analytics,
  WhatsApp updates.
