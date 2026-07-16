# Eval Report — clinic voice agent

Generated: 2026-07-16T18:50:34.020749+00:00  ·  Agent: `agent_630513dce1344a7f64f18ae9c8`  ·  Scenarios: 12

## Scenario results

| Scenario | Lang | Deterministic | Retention | Language | Rubric | Turns |
|---|---|---|---|---|---|---|
| book_happy_en | en | PASS | 0.50 ✗ | 1.00 ✓ | — | 4/9 |
| book_happy_hi | hi | PASS | 1.00 ✓ | 0.84 ✓ | — | 4/9 |
| book_fuzzy_hinglish | hinglish | PASS | 0.75 ✓ | 0.98 ✓ | — | 4/9 |
| earliest_any_branch_en | en | PASS | 1.00 ✓ | 1.00 ✓ | 1.00 ✓ | 3/9 |
| regression_cancel_all_hi | hi | PASS | 0.67 ✓ | 0.82 ✓ | — | 3/9 |
| regression_duplicate_booking_en | en | PASS | 1.00 ✓ | 0.97 ✓ | 0.80 ✓ | 2/9 |
| regression_fee_window_hi | hi | PASS | 0.75 ✓ | 0.99 ✓ | 1.00 ✓ | 4/9 |
| fee_not_mentioned_outside_window_en | en | PASS | 1.00 ✓ | 1.00 ✓ | 0.61 ✓ | 3/9 |
| family_disambiguation_en | en | PASS | 0.60 ✓ | 1.00 ✓ | 0.99 ✓ | 6/9 |
| regression_no_denial_continuity_hi | hi | PASS | 1.00 ✓ | 0.41 ✗ | 1.00 ✓ | 3/9 |
| escalation_human_hinglish | hinglish | PASS | 1.00 ✓ | 0.94 ✓ | 0.98 ✓ | 4/9 |
| identity_and_memory_en | en | PASS | 0.33 ✗ | 0.99 ✓ | 0.82 ✓ | 3/9 |

### Check details (failures only)

_All deterministic checks passed._

## Per-language aggregates

| Language | Scenarios | Deterministic pass | Avg turns |
|---|---|---|---|
| en | 6 | 6/6 | 3.5 |
| hi | 4 | 4/4 | 3.5 |
| hinglish | 2 | 2/2 | 4.0 |

## Latency (real calls, per language)

Calls analyzed: 21

| Language | Calls | e2e p50 (ms) | e2e max (ms) | LLM p50 (ms) | TTS p50 (ms) |
|---|---|---|---|---|---|
| mixed | 20 | 1690 | 6465 | 936 | 176 |
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
4. **Latency under no load.** All latency data comes from single concurrent calls; evaluator
   traffic patterns may differ. Retell's e2e also excludes the caller-side network leg
   (~+250-350ms from India).
5. **Availability nondeterminism.** Scenarios run against live Cliniko availability; a scenario
   could fail if the clinic calendar is fully booked for the searched window. Fixtures pin what
   can be pinned; searches use near-term windows that are normally open.
