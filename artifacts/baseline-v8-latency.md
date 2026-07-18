# Latency & conversation-quality baseline — agent v8 (18 July 2026)

Snapshot taken before the v2 conversation-quality work (turn-taking tuning, ASR
keyword boosting, prompt revision). All numbers come from the Retell call API over
the most recent 13 real phone calls (web calls excluded); "per-call pX" means the
platform's per-call latency percentile, aggregated across calls.

## Voice-to-voice latency (phone calls, n=13)

| metric | mean | median | min | max |
|---|---|---|---|---|
| e2e p50 (ms) | 2116 | 1875 | 1335 | 3700 |
| e2e p90 (ms) | 3446 | 3341 | 1744 | 5509 |
| LLM p50 (ms) | 942 | 918 | 757 | 1191 |
| call duration (s) | 140 | 116 | 37 | 431 |

Longest real call (7 min 11 s, Hindi, book → reschedule across branches):
e2e p50 1969 ms / p90 3717 ms / max 4220 ms; LLM p50 922 ms; TTS p50 177 ms.

Note: Retell's e2e metric excludes the caller-side network leg (~250–350 ms extra
perceived from India on the US number).

## Known conversation-quality defects at v8 (from real-call transcript review)

1. Streaming ASR mangles Indian proper nouns: "Wilson Garden" repeatedly
   mis-transcribed; a caller's name was captured as a wrong Devanagari string and
   written to the PMS that way.
2. Robotic delivery: turns exceed the 2-sentence style rule in Hindi; identical
   template questions repeated up to 4× in one call; slot details re-confirmed in
   full up to 3× before booking.
3. Interruption lag: after caller barge-in, the agent emits fragments of its
   pre-interruption sentence; long p90 turns cause callers to re-speak and collide.
4. Idle reminder re-reads the entire option list verbatim instead of a short nudge.

## Agent v8 config being tuned (live values at snapshot time)

interruption_sensitivity 0.7 · enable_backchannel true · responsiveness unset
(default) · reminder_trigger_ms / reminder_max_count unset · boosted_keywords none ·
vocab_specialization medical · stt_mode unset · denoising_mode unset ·
voice 11labs-Monika · LLM gpt-4.1, temperature 0, high-priority.

## Improvement targets for the tuning round (measured against this baseline)

- Overlap/interruption incidents down ≥25%; full-detail repeated confirmations
  down ≥30% (transcript review over comparable call sets).
- Zero Devanagari patient names written to the PMS (backend transliteration gate).
- No regression in booking success (12/12 eval scenarios) or e2e p90.
