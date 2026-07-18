# Eval Report — clinic voice agent

Generated: 2026-07-18T19:11:11.327441+00:00  ·  Agent: `agent_630513dce1344a7f64f18ae9c8`  ·  Scenarios: 16

## Scenario results

| Scenario | Lang | Deterministic | Retention | Language | Rubric | Turns |
|---|---|---|---|---|---|---|
| book_happy_en | en | PASS | 0.67 ✓ | 0.35 ✗ | — | 6/9 |
| book_happy_hi | hi | PASS | 0.60 ✓ | 0.71 ✓ | — | 5/9 |
| book_fuzzy_hinglish | hinglish | PASS | 1.00 ✓ | 0.89 ✓ | — | 5/9 |
| earliest_any_branch_en | en | PASS | 1.00 ✓ | 1.00 ✓ | 1.00 ✓ | 4/9 |
| regression_cancel_all_hi | hi | PASS | 1.00 ✓ | 0.93 ✓ | — | 3/9 |
| regression_duplicate_booking_en | en | PASS | 1.00 ✓ | 0.86 ✓ | 0.48 ✗ | 6/9 |
| regression_fee_window_hi | hi | PASS | 0.75 ✓ | 0.54 ✗ | 0.98 ✓ | 4/9 |
| fee_not_mentioned_outside_window_en | en | PASS | 1.00 ✓ | 1.00 ✓ | 1.00 ✓ | 4/9 |
| family_disambiguation_en | en | PASS | 0.83 ✓ | 1.00 ✓ | 1.00 ✓ | 7/9 |
| regression_no_denial_continuity_hi | hi | PASS | 1.00 ✓ | 1.00 ✓ | 0.34 ✗ | 5/9 |
| escalation_human_hinglish | hinglish | PASS | 0.80 ✓ | 0.98 ✓ | 0.93 ✓ | 7/9 |
| identity_and_memory_en | en | PASS | 0.40 ✗ | 0.52 ✗ | 0.57 ✗ | 5/9 |
| regression_name_devanagari_hi | hi | PASS | 0.83 ✓ | 0.69 ✓ | — | 7/9 |
| regression_name_implausible_en | en | PASS | 0.57 ✗ | 0.54 ✗ | — | 8/9 |
| verify_otp_cancel_hi | hi | PASS | 0.50 ✗ | 0.63 ✓ | 0.39 ✗ | 3/9 |
| verify_wrong_code_no_leak_en | en | PASS | 0.67 ✓ | 0.90 ✓ | 0.39 ✗ | 4/9 |

### Check details (failures only)

_All deterministic checks passed._

## Per-language aggregates

| Language | Scenarios | Deterministic pass | Avg turns |
|---|---|---|---|
| en | 8 | 8/8 | 5.5 |
| hi | 6 | 6/6 | 4.5 |
| hinglish | 2 | 2/2 | 6.0 |

## Latency (real calls, per language)

Calls analyzed: 30

| Language | Calls | e2e p50 (ms) | e2e max (ms) | LLM p50 (ms) | TTS p50 (ms) |
|---|---|---|---|---|---|
| en | 1 | 3100 | 14605 | 707 | 170 |
| mixed | 28 | 1708 | 6465 | 920 | 176 |
| hi | 1 | 1650 | 4747 | 1046 | 186 |

> Latency values are Retell-measured per-call percentiles aggregated across calls. Retell's e2e EXCLUDES the caller-side network leg: callers dialing from India over the US number (or WebRTC from India) experience roughly +250-350ms on top of these figures. ASR component latency is not separately exposed by the platform; it is included in e2e.

## Where this harness gives false confidence (read before trusting the numbers)

1. **Text-mode blind spot.** Scenario conversations run text-to-text through the same LLM,
   prompt, tools, backend and database as live calls — but they bypass ASR, TTS and telephony.
   Production voice failures (mishearing Indian names/entities, TTS mispronunciation,
   barge-in glitches) are invisible here. Real-call latency is reported separately, and live
   scripted calls (scripts/live_call_checklist.md) remain the truth tier.
2. **Cooperative simulated users.** The gpt-4o-mini patient follows its persona and rarely
   mumbles, interrupts, or changes its mind erratically the way real callers do.
3. **LLM judges are imperfect.** Judged scores (language discipline, scenario rubrics) carry
   known biases; deterministic tool-trace and database assertions are the load-bearing checks,
   judges are advisory.
4. **Latency under no load.** All latency data comes from single concurrent calls; real
   traffic patterns may differ. Retell's e2e also excludes the caller-side network leg
   (~+250-350ms from India).
5. **Availability nondeterminism.** Scenarios run against live Cliniko availability; a scenario
   could fail if the clinic calendar is fully booked for the searched window. Fixtures pin what
   can be pinned; searches use near-term windows that are normally open.
