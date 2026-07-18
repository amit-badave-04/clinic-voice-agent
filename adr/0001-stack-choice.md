# ADR-0001: Voice stack — stay on Retell, tune components, keep GPT-4.1

**Status:** Accepted (July 2026) · **Decides:** platform, LLM, STT, TTS strategy
for v2, and the go/no-go gate for an India-hosted rebuild.

## Context

v1 shipped on Retell (managed STT → GPT-4.1 → ElevenLabs, US-hosted) with a
FastAPI/Postgres integrity backend. Live evaluation found the transactional
layer solid but surfaced voice-layer problems: Indian proper nouns mangled by
ASR ("Wilson Garden" → garbage, a caller booked under a wrong Devanagari
name), Hindi speech intermittently transcribed as Spanish, laggy interruption
handling, robotic delivery. A research round (10 deep-research reports)
proposed everything from parameter tuning to a full India-hosted rebuild on
LiveKit/Pipecat with Sarvam engines.

## Decision 1 — Platform: stay on Retell; Mumbai rebuild is gated, not planned

Tuning beat replatforming on evidence: the observed failures were addressable
in place (see the measured record below), while the rebuild path carries solo-
operator SRE burden (SIP/ICE/RTP operations) that the research itself flagged
as its strongest counter-argument.

**Go/no-go gate for a LiveKit/Pipecat + Sarvam Mumbai prototype** — pursue only
if, after the in-platform STT levers are exhausted (accurate mode — live since
v10 — then a `custom_stt_config: assemblyai` trial), live Hindi calls still
show: proper-noun/entity mangling at a rate that forces read-back loops, or
Hindi→Spanish language drift, or sustained e2e p90 > 3.5 s. The prototype
would be a separate branch sharing this repo's eval scenarios, compared
head-to-head before any migration decision.

## Decision 2 — LLM: GPT-4.1, temperature 0

Tool-calling reliability is the binding constraint: the agent's value is
transactional integrity, and every eval scenario asserts exact tool traces.
The research round's own conclusion (R14) was that indigenous LLMs do not yet
match GPT-4.1-class strict tool-calling; the "downgrade to a nano-tier for
TTFT" suggestion (R12) is testable cheaply, so it is a standing A/B rather
than a guess:

    EVAL_MODEL_OVERRIDE=gpt-4.1-mini python -m evals.run_evals --skip-judges

Gate to switch: identical deterministic pass rate (16/16) across two runs AND
no average-turn inflation > 20%, for a ≥ 300 ms measured TTFT gain. Until a
candidate clears that, latency work targets the STT/turn-taking layers, where
the live-call evidence actually pointed.

## Decision 3 — STT: Retell-managed, accuracy-first; Sarvam is replatform-only

`stt_mode: accurate` has been live since v10 (response to the Spanish-drift
finding), with `boosted_keywords` carrying branch/locality/practitioner names
plus the per-call `{{patient_names}}` variable. Verified against the live API
(18 July 2026): `custom_stt_config` accepts only a closed provider list —
`azure | deepgram | soniox | assemblyai` — and no custom WebSocket endpoint,
so **Sarvam cannot plug into Retell**; it is only reachable via the gated
rebuild above. Next in-platform lever if entity errors persist: provider
`assemblyai` (benchmarked at roughly one-third of Deepgram's entity error rate
on names/places in the research round's data, with the caveat that its
dialectal-Hindi performance is unproven — hence a trial, not a default).

The deterministic backstop is deliberately NOT in the STT layer: the backend
name-integrity gate (romanization, plausibility, fuzzy roster match) and the
read-back protocol guarantee record correctness regardless of ASR quality.

## Decision 4 — TTS: ElevenLabs Monika primary, cross-provider fallback

`11labs-Monika` (en-IN) remains primary on measured TTS TTFB of ~175–185 ms
p50 across all recorded calls — TTS is not the latency bottleneck (the LLM
p50 is ~0.9–1.2 s). `openai-Monika` is configured as mid-call fallback so a
provider outage degrades the voice, not the call. A Cartesia trial is a
listening test, not a metric test: set `RETELL_VOICE_ID`, re-sync, run the
five scripted Hinglish lines from `scripts/live_call_checklist.md`, judge
naturalness + code-switching; revert by clearing the variable.

## Decision 5 — Standing knobs under measurement

- `vocab_specialization`: `medical` (hypothesis from research: `general` may
  transcribe Indian proper nouns better; test only alongside a live-call
  entity check, never blind).
- `model_temperature`: 0 (a 0.15–0.25 trial for delivery variety is allowed
  only with the full deterministic suite green as the gate).

## Measured record (what this ADR stands on)

- **Latency**: v8 baseline (13 calls): per-call e2e p50 mean 2116 ms / p90
  mean 3446 ms; LLM p50 ~0.9–1.2 s; TTS p50 ~175 ms. Post-tuning calls (v9+)
  on simple flows: e2e p50 1300–1750 ms; heavy tool-turn calls unchanged
  (~2.5 s p50) — consistent with the LLM+tool path, not TTS, being the
  bottleneck. (`artifacts/baseline-v8-latency.md`.)
- **Behavior**: the four live-call complaints (interruptions, repetition,
  branch friction, name mangling) each traced to a specific layer and were
  fixed by config (v9/v10), prompt rules 14–17, or deterministic backend
  gates — none required leaving the platform. Eval suite grew 12 → 16
  scenarios, deterministic checks green throughout (transcript-verified
  judge noise documented in `evals/results/`).
- **Platform facts verified against live docs/SDK** (July 2026): closed STT
  provider list (above); inbound call rejection requires an unbound number
  (kill-switch design); `boosted_keywords` accepts dynamic variables;
  `reminder_message` and `normalize_for_speech` do not exist despite research
  claims to the contrary.

## Consequences

The demo stays on one always-warm US stack with per-component escape hatches
(STT provider switch, TTS fallback + trial protocol, LLM A/B lever) and one
explicit, evidence-gated exit (Mumbai prototype). India-side latency
(~250–350 ms caller leg) is accepted as a demo-scale trade-off and documented
in the README rather than engineered away prematurely.
