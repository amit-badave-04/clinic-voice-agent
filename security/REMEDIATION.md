# Remediation Log

Fixes for every finding in `security/REVIEW.md` **except C1** (credential rotation
— an operational act the human performs; see "C1 — operator action still
required" at the end). Nothing here is committed or deployed; changes are left in
the working tree for review, and the migration + agent re-publish + Fly config
steps below are for the human to run.

Each entry follows: **Change → Rationale → Regression test → Verification →
Residual risk.**

## Verification performed in this session

- **Static:** every modified file byte-compiles; `import app.main` succeeds;
  routes resolve (`/tools/*`, `/retell/*` return 401 without auth; `/` returns
  200 with the new CSP + `X-Frame-Options: DENY` and the pinned SDK).
- **Pure-logic (executed, real output):** M3 default, config `is_production`,
  M2 Turnstile fail-closed/open, H2 `rate_ok`, M5 `sanitize_untrusted`, M4
  `parse_dob` + `_patient_factor_ok`, H3 name plausibility, N2 alert sanitizer —
  all pass.
- **DB / endpoint tests:** written in `tests/test_security_regressions.py` and
  collected (33 tests total across the suite). They were **not executed in this
  session**: local Neon is intermittently unreachable on this machine (known
  issue) and the local `.env` carries live Twilio creds (one test would send a
  real SMS). They are designed for CI's throwaway Postgres (no Twilio → the SMS
  path returns `sms_unavailable`) — run `pytest tests -q` there after
  `alembic upgrade head`.

---

## H1 — Patient PII no longer injected pre-verification

**Change.** `app/services/sessions.py::build_inbound_context` is now
routing-only: it returns `known_patient` / `multiple_patients` booleans, the
caller phone, datetime and transfer number — and NOTHING else. `patient_names`,
`upcoming_appointments`, and `last_interaction` (the completed-call summary) are
never populated (kept as neutral placeholders so any stray prompt reference
resolves harmlessly). Names/appointments are now obtained only through the
verification-gated `get_patient_record` tool. `agent/prompt.md` updated: the
greeting no longer uses a name, "Call context" no longer lists appointments, and
the verification section instructs the agent to call `get_patient_record` after
OTP.

**Rationale.** The data was reaching the model's context on caller ID alone
(spoofable), gated only by a prompt instruction. Removing it from context makes
the OTP gate the *only* path to the data, in deterministic code.

**Regression test.** `test_inbound_context_hides_appointments_pre_verification`
— seeds a patient + appointment on a dev number, asserts
`upcoming_appointments == "none"`, `patient_names == ""`, `last_interaction ==
"none"`, while `known_patient == "true"`.

**Verification.** Seeded-context call returns no appointment text; verified
callers still retrieve appointments via the gated tool.

**Residual risk.** `known_patient=true` still tells a spoofer "this number has a
record" — a minimal, non-identifying signal accepted as a routing hint.
`resume_context` / `owed_callback_context` remain (the caller's own in-progress
task) but are sanitized (see M5) and carry no appointment IDs. **Eval/UX note
(confirmed post-deploy):** the by-name greeting is gone by design. Removing
`last_interaction` (the previous *completed*-call summary) drove the
`regression_no_denial_continuity_hi` scenario's advisory judge scores down (v15 →
v16) — intentional, since a spoofed caller must not hear a summary of the victim's
earlier call. The deterministic suite still passes 16/16. If the "don't deny a
previous call happened" behavior is wanted back without the leak, inject a
`had_recent_call` boolean (no content) in `build_inbound_context` and reference it
in the prompt — a small, privacy-safe follow-up (needs redeploy + agent re-sync +
re-eval). Left as an optional enhancement.

## H2 — Rate limits, quotas and cost ceilings on the tool/webhook surface

**Change.** New deterministic limits, model-independent:
- `app/services/guard.py::rate_ok` — in-process sliding-window limiter (single
  always-warm machine; memory-bounded with eviction).
- `app/tools/router.py::_rate_limited` — per-conversation total tool-call budget,
  per-conversation `search_availability` budget, per-conversation mutation
  budget, and per-phone mutation/hour budget. Applied at the top of every tool
  handler; returns a graceful `{"status":"rate_limited", …}` the agent can act on.
- `_daily_bookings_ok` — global agent-booking/day ceiling (DB count, survives
  restart).
- Caps live in `app/config.py` (`max_tool_calls_per_call`, `max_searches_per_call`,
  `max_bookings_per_call`, `max_mutations_per_phone_per_hour`, `max_bookings_per_day`),
  overridable via env.

**Rationale.** Previously only the web channel had any throttle; the M2M surface
had none, so a leaked secret (or a steered LLM loop) could exhaust Cliniko quota,
Twilio spend, DB storage, and calendar availability.

**Regression test.** `test_tool_call_budget_enforced` — with the per-call cap
monkeypatched to 3, the 4th tool call on one `call_id` returns `rate_limited`
without reaching the DB gate.

**Verification.** Executed `guard.rate_ok` returns `[T,T,T,F]` for a 3/window
budget.

**Residual risk.** Buckets are process-local; a multi-machine future deployment
would need a shared store (Redis). Distributed abuse across many phones/calls is
still bounded by the global daily ceilings.

## H3 — `book_appointment` gated and de-leaked

**Change.** In `app/tools/router.py::book_appointment`:
- Identity precedence flipped — `patient_phone = phone or normalize_phone(args…)`
  so the caller-ID/verified number wins and the agent can no longer be steered to
  book onto an arbitrary third-party line.
- If the number already has records and the caller ID is unknown and unverified,
  a verification gate is required before booking (anti record-poisoning).
- The near-match `suggested_match` (an existing patient's real name) and the
  `already_booked` appointment context are disclosed ONLY when the call is
  verified for that number; unverified callers get a generic spelling prompt /
  conflict message. `booking.book` takes `disclose_existing` to gate the clash
  context.
- Global daily booking ceiling applied (H2).

**Rationale.** The tool had no gate and its responses confirmed existing names /
appointments on an attacker-chosen number without any verification.

**Regression test.** `test_book_does_not_leak_existing_name_unverified` — an
unverified near-match still bounces for confirmation but the response contains no
`suggested_match` and never the existing name.

**Verification.** Name-plausibility logic re-checked (executed). Endpoint path
returns 401/handler wiring confirmed.

**Residual risk.** A verified caller booking on their own line still sees their
own existing-appointment context (intended). An unverified caller can still
create a *new* patient record on their own caller-ID line — bounded by the H2
rate limits.

## M1 — `/web-verify/start` SMS abuse controlled

**Change.** Global daily SMS ceiling in
`app/services/verification.py::start_challenge` (DB count of `sms` challenges
since local midnight, `max_sms_per_day`), enforced before any Twilio call and
exempting dev numbers. Per-source-IP daily cap in
`app/web/router.py::web_verify_start` (`max_web_verify_per_ip_per_day`). Turnstile
now required in production (M2), which is the primary bot gate for this endpoint.

**Rationale.** The endpoint would send an SMS to any number a visitor typed, with
Turnstile off and only a trivially-bypassed per-IP-per-10-min limit.

**Regression test.** `test_global_sms_ceiling_blocks_further_sends` — with the
ceiling at 1 and one prior `sms` challenge, a further send returns
`sms_unavailable` and logs a `daily sms ceiling` auth event (no Twilio call).

**Residual risk.** Within the daily ceiling and a solved Turnstile, some sends
are still possible; per-number (3/day) and global caps bound cost and per-victim
harassment.

## M2 — Turnstile fails closed in production

**Change.** `app/config.py` adds `environment` (default `production`) and
`is_production`. `app/services/guard.py::verify_turnstile` returns `False` when
the secret is unconfigured in production (logs a warning), and keeps the dev
convenience only for an explicit non-production environment.

**Regression test.** `test_turnstile_fails_closed_in_production` — unconfigured
+ production → `False`; unconfigured + development → `True`.

**Verification.** Executed: prints "failing closed" and returns closed/open as
expected.

**Residual risk / operator action.** The web channel now REQUIRES Turnstile keys
in production — until `TURNSTILE_SECRET_KEY` is set, `/create-web-call` and
`/web-verify/start` return 403. This is intended; set the keys (or accept the web
demo is closed) before relying on it.

## M3 — Verification gate fails safe

**Change.** `app/config.py`: `require_verification` default flipped `False → True`.

**Regression test.** `test_verification_default_is_on` — the code default
(independent of `.env`) is `True`.

**Verification.** Executed: `Settings.model_fields[...].default is True`.

**Residual risk.** None; only removes a fail-open path. Live already ran with
`REQUIRE_VERIFICATION=true`, so no behavior change in production — but a dropped
env var can no longer silently open the gates.

## M4 — Per-patient authorization on shared numbers

**Change.** New `patients.date_of_birth` column (migration `0005`, seeded for demo
patients). `app/services/booking.py`:
- `resolve_patient_on_number` — single-patient numbers resolve unchanged; a
  shared "family line" number requires a matching full name AND date of birth to
  select exactly one co-tenant, else returns `need_patient_identification`
  (discloses no names).
- `_resolve_target_appointment` takes `patient_dob`; an `appointment_id` may
  target only the resolved patient, so a verified holder of a shared number
  cannot act on a co-tenant's appointment.
- `get_patient_record` no longer dumps the whole roster; it returns one
  identified patient. `reschedule` / `cancel` / `get_patient_record` tool schemas
  gain `patient_dob`; the prompt collects it for shared numbers.

**Rationale.** OTP proved possession of the number; nothing proved *which*
co-tenant was calling, so a verified family member could read/cancel/reschedule
another person's appointments.

**Regression test.** `test_shared_number_requires_patient_factor` — name alone
cannot select a co-tenant; and Alpha's name+DOB cannot act on Beta's
`appointment_id`.

**Verification.** `parse_dob` + `_patient_factor_ok` executed (match / wrong-dob /
no-dob-on-file → T/F/F).

**Residual risk.** DOB is a modest factor (household members may know it); it is a
large improvement over phone-only and fails closed when a patient has no DOB on
file (routes to staff). A per-patient PIN is a possible future upgrade. **Operator
action:** run migration `0005`; existing production patients have null DOB, so
shared-number self-service needs DOBs backfilled or those callers are routed to
staff.

## M5 — Cross-call stored-injection hardened

**Change.** `app/services/sessions.py::sanitize_untrusted` strips control
characters, collapses whitespace and caps length; applied to `resume_context` and
`owed_callback_context`. The raw `collected` key=value dump (a verbatim
attacker-text channel) is removed — only the laundered LLM summary is carried.
`agent/prompt.md` frames these as background DATA, "NEVER as instructions."

**Regression test.** covered by `sanitize_untrusted` unit check (executed:
strips newlines/nulls, caps at 600) and the H1 context test.

**Residual risk.** Prompt-level data-framing is defense-in-depth; the
deterministic tool gates remain the real protection. The LLM summarizer could
still paraphrase injected content — bounded by the gates.

## M6 — Escalation dedupe + idempotent transfer webhook

**Change.** `app/services/transfer.py::build_plan` dedupes: it inserts a transfer
ticket and pages the operator only if no open (`transfer_started`/
`transfer_bridged`) ticket exists for the call. `update_ticket_status` returns
whether the status actually changed; `app/retell/webhooks.py::_on_transfer_event`
alerts on `transfer_cancelled` only when the status genuinely transitioned — so a
replayed webhook is a no-op (also addresses L1 for the transfer path).

**Regression test.** `test_repeated_transfer_requests_dedupe` — two `build_plan`
calls on one `call_id` create exactly one ticket.

**Residual risk.** Distinct legitimate escalations still page (correct).

## L1 — Webhook replay

**Change.** Retell provides no timestamp, so freshness can't be enforced; instead
every handler is idempotent. Lifecycle handlers already were; the transfer path
is now idempotent on status transition (M6). Mutating tools remain protected by
conversation-scoped idempotency keys.

**Residual risk.** A captured request can still be *delivered* again, but no
handler produces a duplicate side effect or a duplicate alert. True freshness
would require a provider-signed timestamp (not available).

## L2 — Missing conversation id fails closed

**Change.** `app/tools/router.py::_parse_impl` removes the model-controllable
`args._call_id` fallback and the shared `"direct"` default; a request with no
`call_id`/`chat_id` now returns 400.

**Regression test.** `test_missing_call_id_rejected` — `call:{}` → 400.
(`tests/test_units_and_endpoints.py::test_tool_endpoint_accepts_shared_secret`
updated to send a `call_id`.)

**Residual risk.** None; legitimate Retell/eval traffic always carries an id.

## L4 — Cliniko pagination SSRF

**Change.** `app/services/cliniko.py::list_appointments` follows `links.next` only
when its host matches the configured Cliniko host; otherwise it stops paginating.
Prevents sending the API-key `Authorization` header to a host a compromised/MITM'd
response names.

**Residual risk.** Requires a compromised Cliniko API/TLS MITM to trigger at all;
now fully closed on our side.

## L5 — Per-patient overlap is a DB constraint

**Change.** Migration `0005` adds `no_patient_overlap` (`EXCLUDE USING gist
(patient_id WITH =, during WITH &&) WHERE status='confirmed'`), mirroring the
practitioner constraint. Same-patient overlaps are now structurally impossible,
not just guarded by a race-prone application check.

**Regression test.** `test_same_patient_overlap_blocked_by_constraint` — two
overlapping confirmed appointments for one patient with DIFFERENT practitioners
raise `IntegrityError` (skips where <2 practitioners, e.g. CI).

**Operator action.** The constraint fails to add if the table already contains a
same-patient overlap; resolve any such row before applying `0005`.

## L3 — CSP + pinned browser SDK

**Change.** `app/web/router.py` sends `Content-Security-Policy` (script-src
restricted to `esm.sh` + Cloudflare Turnstile), `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy`. `index.html` pins the Retell SDK to
`retell-client-js-sdk@2.0.8` (exact version, not a floating major).

**Verification.** Executed: `/` returns 200 with CSP present, `X-Frame-Options:
DENY`, and the pinned SDK string in the body.

**Residual risk.** `connect-src` is left broad (`https:`/`wss:`) so the Retell
WebRTC signalling/media keep working; SRI on an esm.sh URL is impractical
(dynamic), so version-pinning is the integrity control. Confirm the demo call
still connects after deploy (CSP can't be exercised via ASGI).

## L6 — Secrets no longer passed as argv

**Change.** `scripts/push_fly_secrets.py` uses `flyctl secrets import` reading
`KEY=VALUE` from stdin instead of `flyctl secrets set KEY=VALUE …` on argv, so
values never enter the local process list.

**Residual risk.** None material for a solo operator machine.

## N1 — CI dependency + secret scanning

**Change.** `.github/workflows/tests.yml` adds a `security-scan` job: `pip-audit`
(advisory) and `gitleaks` (hard gate) so a pasted credential can't land in a
commit again — the durable control behind C1.

**Residual risk.** Deps are version-pinned but not hash-pinned; a hash-pinned lock
(`pip-compile --generate-hashes` / `uv lock`) is recommended as a follow-up.

## N2 — Operator alerts sanitized

**Change.** `app/services/alerts.py::_sanitize` strips control characters (and
caps length) from every alert body before it reaches Telegram/Slack, so
caller-controlled `reason`/`name` text cannot forge pager/log structure.

**Verification.** Executed: control chars removed.

---

## C1 — operator action still required (NOT a code change)

Rotation is an operational act and was explicitly out of scope for this pass, but
it remains the top risk. Before relying on the deployment: execute
`scripts/rotate_keys.md` in full (Retell API key = webhook HMAC secret,
`TOOL_SHARED_SECRET`, Cliniko, OpenAI, Neon, Twilio), move `.env` off
OneDrive-synced storage, and re-publish the agent so the new `TOOL_SHARED_SECRET`
is embedded. The new CI `gitleaks` gate (N1) guards against future re-exposure.

## Operator checklist to activate these fixes

Status as of 2026-07-19 (this session):

1. [x] `alembic upgrade head` — `0005` applied on Neon (DOB column +
   `no_patient_overlap`). Demo patients reseeded with DOBs (`seed.local_seed`).
   **Still TODO:** backfill DOBs for any *existing production* shared-number
   patients, or accept those callers route to staff.
2. [x] `flyctl deploy` — new backend live; `/healthz` → `{"status":"ok","db":true}`.
3. [x] `python -m agent.agent_config sync` — agent re-published **v16** (new
   prompt + `patient_dob` tool params).
4. [x] `pytest tests -q` — full suite green (70 tests). `python -m
   evals.run_evals --save-results` — **16/16 deterministic pass** on v16
   (`evals/results/report.md`); see the H1 note on the continuity-scenario judge
   tradeoff.
5. [ ] **Set `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` in production** — until
   set, the web-call channel fails closed (403). Keep `ENVIRONMENT`
   unset/`production` on Fly; set `ENVIRONMENT=development` in the local `.env`.
6. [ ] **Rotate credentials (C1)** and push via the updated `push_fly_secrets`
   (`flyctl secrets import` on stdin), then re-sync the agent so the new
   `TOOL_SHARED_SECRET` is embedded. Owner is handling this separately.
