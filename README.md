# Clinic Voice Agent — bilingual AI receptionist (English + हिंदी)

A production-style **voice AI receptionist** for a real two-branch physiotherapy clinic in
Bengaluru. Patients call a real phone number (or use a browser call), speak naturally in
English, Hindi, or mixed Hinglish, and book / reschedule / cancel appointments — checked
against live practice-management (Cliniko) availability and enforced by a Postgres backend
that makes double-booking structurally impossible.

**📞 Live demo:** call `+1 (628) 356-4436` · or use the [browser call page](https://clinic-voice-agent.fly.dev/) (no dialing cost, works worldwide)

> The clinic modeled here (branches, practitioners, timings, ₹400 fee) is **real, publicly
> sourced data** from Arogya Physiotherapy's website — see [DISCLAIMER.md](DISCLAIMER.md).
> This is an educational demo, not affiliated with the clinic.

---

## What it handles

- **Full appointment lifecycle** — booking, rescheduling, cancellation, conflict resolution
  with live re-checks and graceful alternatives.
- **English + Hindi with mid-sentence code-switching** — real multilingual ASR/LLM/TTS, no
  translation tables. The agent mirrors the caller's language turn by turn.
- **Fuzzy time understanding** — "any Thursday morning", "Mondays and Wednesdays work",
  "after I get off work, around four thirty", "earliest slot anywhere today" all become
  structured search parameters resolved against live availability.
- **Returning callers** — recognized by caller ID before the first "hello" (pre-answer
  webhook), greeted by name with their upcoming appointments in context.
- **Dropped-call resume** — hang up mid-booking, call back, and the agent acknowledges the
  drop and continues where you left off (15-minute window, consumed exactly once).
- **Missed outbound → callback** — if the clinic's call goes unanswered and the patient
  rings back, the agent knows why the clinic called.
- **Family shared numbers** — two patients on one number: the agent asks *who* first.
- **Cross-branch earliest-slot search** — fans out across every practitioner × branch
  concurrently and answers with the true global earliest.
- **Fee policy honesty** — a ₹100 change fee is mentioned *only* when the change falls
  inside the 24-hour policy window.
- **Escalation** — clinical concerns or "I want a human" produce a logged follow-up ticket
  and an honest "someone will call you back" (never a fake transfer), with no medical advice.
- **Bot-identity honesty**, buffer-time-aware slots, IST-correct dates (no UTC drift),
  spoken-form numbers/times in both languages.

## Stack choice & why

| Layer | Choice | Reasoning |
|---|---|---|
| Voice platform | **Retell AI** | Won on operational surface: pre-answer inbound webhook with per-call dynamic variables (returning-caller recognition), per-component latency telemetry via API (this report's latency data), text-mode Chat API against the same agent brain (powers the eval harness), agent-as-code via API, HMAC-signed webhooks, $10 starter credit. The honest trade-off: US-only servers add ~250–350ms perceived latency for India callers, and Deepgram's Hindi entity recognition is weaker than India-native ASR (Sarvam). Bolna was the runner-up — better Hinglish ASR/TTS via native Sarvam integration — but offers no simulation/testing API (the eval harness would have been fully hand-rolled), a beta API-only flow builder, and its India-hosting advantage applies only to enterprise plans. For a solo 3-day build graded on a re-runnable eval harness, Retell's tooling won. |
| LLM | **GPT-4.1** (Retell-hosted, temp 0, high-priority pool) | Strong structured tool-calling at low TTFT; handles Devanagari Hinglish generation natively. |
| STT | **Deepgram multilingual** (`["en-IN","hi-IN"]`, medical vocab) | Only in-platform option with true intra-sentence Hindi/English code-switching. Known limitation: Indian proper nouns ("Bannerghatta") sometimes garble; the LLM recovers from context. |
| TTS | **ElevenLabs "Monika" (en-IN)** | Female Indian-accent voice, natural Devanagari Hindi + English mixing; ~180ms TTFB measured. |
| Telephony | **Twilio US number → Retell via Elastic SIP trunk** | Retell's direct number purchase requires US-ID verification (blocked for Indian individuals); an Indian DID requires business KYC that is impossible in days. A Twilio US number imported over SIP is callable worldwide; the browser call page is the zero-cost fallback. Fully scripted: `python -m scripts.import_twilio_number`. |
| Backend | **FastAPI (async) + Postgres (Neon) on Fly.io** | Deployed in `sjc`, co-located with Retell's US-West infrastructure — tool-call round trips are ~30–80ms, which matters more than caller proximity. Always-warm (`min_machines_running=1`): a cold start mid-call is a fail. |
| PMS | **Cliniko** (30-day trial, 2 businesses = 2 branches) | Real availability engine (working hours, buffers, existing bookings). Its gaps are engineered around — see next section. |

## The backend is the integrity boundary

Cliniko **permits double-bookings, has no webhooks, no idempotency support, and cannot
search patients by phone** — so correctness lives in Postgres:

- **No double-booking, structurally**: appointments carry a `tstzrange` with a GiST
  **exclusion constraint** (`practitioner_id WITH =, during WITH &&`); an overlapping
  confirmed booking is impossible at the database level. Proven by a concurrent-race test.
- **Idempotent tools**: Retell retries failed tool calls; every write derives an idempotency
  key from `(call, tool, args)` and replays return the stored response. Cancels/reschedules
  target explicit `appointment_id`s so "cancel all three" can never collapse into one.
- **Live re-validation**: `book_appointment` re-checks the slot against Cliniko *at write
  time* — LLM context can never confirm a stale slot.
- **Patient overlap guard**: one patient cannot hold two overlapping bookings (prevents
  duplicate re-booking of an existing appointment).
- **Defined PMS-failure behavior**: Cliniko write-back is attempted synchronously (3s);
  on failure the booking stands locally and a **transactional outbox** retries with
  exponential backoff (`FOR UPDATE SKIP LOCKED`).
- **Timezone discipline**: storage is UTC, all human-facing computation in `Asia/Kolkata`;
  regression tests cover the classic "today became tomorrow" edges. (Cliniko quirk handled:
  `available_times` interprets dates in account-local time while everything else is UTC.)

## Architecture

```
Caller (PSTN via Twilio SIP ─or─ browser WebRTC)
        │
   Retell AI (US-West)  — Deepgram multi STT · GPT-4.1 · ElevenLabs TTS
        │  ① call_inbound webhook (pre-answer): phone → patient context, resume
        │     context, owed callbacks, family disambiguation, IST clock
        │  ② tool calls (HMAC-verified): search / book / reschedule / cancel /
        │     patient record / follow-up ticket
        │  ③ call_started / call_ended / call_analyzed webhooks → sessions & summaries
        ▼
   FastAPI on Fly.io (sjc)  ←→  Neon Postgres (us-west-2)   [source of truth]
        ▼
   Cliniko PMS (availability reads · appointment write-back with outbox retry)
```

Agent config is **code** ([agent/prompt.md](agent/prompt.md) + [agent/tools_schema.py](agent/tools_schema.py)),
pushed via `python -m agent.agent_config sync` with proper draft→publish versioning. The
dashboard is never the source of truth.

## Eval harness

```
make eval        # or: python -m evals.run_evals
pytest tests -q  # DB integrity guarantees
```

Three layers, because transcripts alone lie:

1. **Simulated-patient scenarios** (12, tagged en/hi/hinglish) — a gpt-4o-mini persona
   converses with the **production agent brain** over Retell's Chat API: same prompt, same
   tools, same backend, same database, live Cliniko. Every live-testing failure found during
   development is encoded as a named `regression_*` scenario (cancel-all completeness,
   duplicate-booking guard, fee windows, previous-call denial).
2. **Deterministic verification** — tool-trace assertions (search-before-book ordering,
   slot-ID provenance, distinct cancel IDs) plus **direct database truth checks** (the
   booking exists; all three cancellations really happened). LLM judges never get the final
   word on state.
3. **DeepEval judges** (temp-0 gpt-4o-mini) — `KnowledgeRetentionMetric` for redundant
   questions, `ConversationalGEval` for per-language discipline and scenario rubrics.

Plus a **per-language latency report** aggregated from real phone/web calls (Retell
per-call percentiles), and DB-level pytest proofs (concurrent double-booking race,
idempotent replay, cancel disambiguation, fee boundaries, timezone edges).

Committed results: [evals/results/report.md](evals/results/report.md) (produced with
`python -m evals.run_evals --save-results`; ad-hoc runs write to the gitignored
`evals/out/`). The report ends with an honest **false-confidence section** — text-mode
scenarios bypass ASR/TTS/telephony, simulated users are too cooperative, judges are
biased, latency was measured under no load.

> ⚠️ The eval suite books and cancels **real appointments on the live Cliniko calendar**
> (with synthetic patients, cleaned up afterwards). Don't run it while someone is
> live-testing the phone number — a scenario could transiently occupy a real slot.

## Measured latency

See the committed [eval report](evals/results/report.md) for current per-language numbers
from real calls. Typical figures during development: **e2e p50 ≈ 1.4–1.9s** (Retell-measured;
excludes the caller-side India↔US leg of ~250–350ms), LLM p50 ≈ 0.9–1.2s (the dominant
component; heavy multi-tool turns spike to 3–4s), TTS ≈ 180ms. Latency posture: high-priority
LLM pool, tool-level static fillers that mask tool round-trips, backend co-located with the
platform, 30s availability cache with write-time re-validation.

## Reproduce it

Prereqs: Python 3.11, accounts for Retell, OpenAI, Cliniko (trial), Neon, Fly.io, Twilio
(only for the PSTN number).

```bash
git clone https://github.com/amit-badave-04/clinic-voice-agent && cd clinic-voice-agent
conda env create -f environment.yml && conda activate voice-ai-agent   # or: pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                      # fill in keys (comments explain each)
alembic upgrade head                      # schema (incl. exclusion constraint)
python -m seed.cliniko_seed               # branches + appointment types in Cliniko
#   → add practitioners in the Cliniko UI (SETUP_CLINIKO.md — API can't create them)
python -m seed.cliniko_seed               # re-run: links practitioner IDs
python -m seed.local_seed                 # demo patients + fee policy
flyctl launch --no-deploy && flyctl secrets set ... && flyctl deploy   # or any always-warm host
python -m agent.agent_config sync         # create/update + publish the Retell agent
python -m scripts.import_twilio_number    # optional: PSTN number via Twilio SIP import
python -m scripts.smoke_cliniko           # sanity: slots per practitioner
make eval                                 # the harness (only needs .env)
```

Useful scripts: `scripts/dump_calls.py` (recent calls + latency), `scripts/dump_transcript.py`,
`scripts/outbound_call.py` (missed-call/callback demo), `scripts/live_call_checklist.md`
(manual scenario checklist).

## Known limitations (honest list)

- **Caller ID is trusted as identity.** The agent recognizes returning patients by
  phone number (the assignment's requirement), and the web page's phone field simulates
  caller ID for browser demos — so anyone asserting a number can hear that patient's
  upcoming appointments and change them. PSTN caller ID is itself spoofable. A production
  deployment would verify identity (OTP to the number on file, DOB check) before
  disclosing or modifying records. Blunted here by a per-IP rate limit on the web-call
  endpoint; accepted as a documented demo trade-off.
- **Demo-scale abuse protection only**: the web-call endpoint is rate-limited per IP,
  but there is no global quota, CAPTCHA, or WAF. Real deployments need all three.
- **Staff notification is out of scope**: escalations and failed PMS write-backs are
  logged durably (`followup_tickets`, `outbox.status='failed'`) but nobody is paged;
  the assignment requires logging, not alerting.
- **ASR on Indian proper nouns**: Deepgram occasionally mangles branch/locality names
  ("Bannerghatta" → garble); the LLM recovers from context, but an India-native ASR
  (Sarvam via Bolna) would be materially better. This was the main argument for Bolna and
  remains the top improvement candidate.
- **India-caller latency**: worst-case heavy turns reach ~3–4s perceived. Fixable with an
  India-hosted stack (enterprise Bolna) or lighter LLM at some tool-reliability cost.
- **Cliniko trial caps active practitioners at 5** → 4 of the clinic's 6 public roster
  doctors are modeled (both dual-branch doctors kept; documented in `seed/arogya_data.py`).
- **Eval harness text-mode blind spots** — declared in the report; real-call spot checks
  via the live checklist remain necessary.
- **Single language pair** (English/Hindi) by design; the platform language array and
  prompt rules would extend to more.
- **No live transfer** — by scope: escalation logs a ticket and promises a callback,
  which the agent states honestly.

## Repo map

```
app/        FastAPI: webhooks, tool endpoints, services (availability, booking,
            sessions, outbox, Cliniko client), schema + migrations
agent/      prompt.md · tools_schema.py · agent_config.py (agent-as-code)
seed/       sourced clinic data · Cliniko seeder · local seeder
evals/      scenario harness · judges · latency report · reports in out/
tests/      DB integrity proofs (race, idempotency, fees, timezones)
scripts/    number import · outbound call · call dumps · smoke tests · live checklist
```
