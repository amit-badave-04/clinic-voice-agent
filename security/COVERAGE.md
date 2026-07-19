# Review Coverage Ledger

Adversarial security review of `clinic-voice-agent/`, 2026-07-19. Every source file
in the repo is accounted for below. `.pyc`/`__pycache__` are generated artifacts and
excluded. No final verdict was issued while relevant files remained unreviewed.

Legend: **Full** = read line-by-line; **Swept** = read via targeted grep for
SQLi / shell-exec / secret-handling / injection patterns (non-request-path tooling);
**Config** = inspected as configuration; **Gen** = generated/binary/excluded.

## Request-path & security-critical (Full)

| File | Disposition | Notes / findings anchored here |
|---|---|---|
| `app/tools/router.py` | Full | H2, H3, M6, L2; verification gate correct & phone-scoped |
| `app/retell/webhooks.py` | Full | L1, M6; lifecycle idempotency correct |
| `app/retell/security.py` | Full | **C1**, L1; HMAC over raw body, size cap, constant-time compare — mechanism correct |
| `app/services/verification.py` | Full | Core OTP — **correct**; bound to (call_id,phone); OTP only to on-file number |
| `app/services/booking.py` | Full | H3, L5; integrity core strong (idempotency in-txn, live re-validation, GiST) |
| `app/services/sessions.py` | Full | **H1**; pre-answer context injection is the disclosure vector |
| `app/services/guard.py` | Full | M2; Turnstile fail-open |
| `app/services/transfer.py` | Full | M6; business logic (hours/channel) correctly server-side |
| `app/services/names.py` | Full | H3 (suggested_match leak); name gate is language-agnostic (good) |
| `app/services/phone.py` | Full | E.164 normalization correct; garbage→"" |
| `app/services/outbox.py` | Full | Transactional outbox correct; SKIP LOCKED; state derived at send |
| `app/services/availability.py` | Full | H2 (Cliniko fan-out); slot_id opaque token; live re-validation |
| `app/services/cliniko.py` | Full | L4 (pagination host); Basic auth; 200/min handling |
| `app/services/reconcile.py` | Full | Cliniko-content trust (indirect-injection surface, low) |
| `app/services/summarize.py` | Full | M5 (verbatim collected fallback → cross-call injection) |
| `app/services/alerts.py` | Full | N2; best-effort, no parse_mode (no markup injection) |
| `app/services/timeutils.py` | Swept | Pure date/tz logic; no security surface (referenced by tests) |
| `app/web/router.py` | Full | M1, M2; persona/real modes sound; token single-use |
| `app/web/static/index.html` | Full | L3; textContent throughout (no DOM XSS) |
| `app/config.py` | Full | **M3** (require_verification fail-open default); URL normalization |
| `app/db/models.py` | Full | Schema = integrity boundary; multi-patient-per-phone (M4) |
| `app/db/session.py` | Full | asyncpg/PgBouncer/NullPool setup; no issue |
| `app/main.py` | Full | No rate-limit middleware (H2); healthz; background loops |
| `app/db/migrations/versions/0001_initial_schema.py` | Full | GiST exclusion constraint — correct (partial on confirmed) |
| `app/db/migrations/versions/0002_identity_verification.py` | Full | verification tables — correct |
| `app/db/migrations/versions/0003_reconcile_metadata.py` | Full | source_system/externally_modified |
| `app/db/migrations/versions/0004_ticket_status.py` | Full | ticket status column |
| `app/db/migrations/env.py` | Swept | Alembic env; no user input |
| `agent/prompt.md` | Full | H1/M5 context vars; verification is prompt-advisory (not a boundary) |
| `agent/tools_schema.py` | Full | Tool contract; `X-Tool-Secret` embedded (C1); `patient_phone` model-supplied (H3) |
| `agent/agent_config.py` | Swept | Retell agent publishing; reads secrets from settings, no leak |

## Deployment / CI / deps (Config)

| File | Disposition | Notes |
|---|---|---|
| `Dockerfile` | Full | Non-root user; `COPY . .` safe because of `.dockerignore` |
| `.dockerignore` | Full | Correctly excludes `.env`, `.env.*`, `.git` — no secrets in image |
| `fly.toml` | Full | force_https; single machine; no secrets in file |
| `.github/workflows/tests.yml` | Full | Throwaway Postgres, ci-only secret, no deploy, **no dep/secret scan (N1)** |
| `requirements.txt` | Full | Pinned, not hash-pinned (N1) |
| `requirements-dev.txt` | Swept | Test/eval tooling |
| `environment.yml` | Config | conda env spec |
| `alembic.ini` | Config | migration config; no secret |
| `pytest.ini` | Config | asyncio mode |
| `Makefile` | Full | Plain command aliases; no secret |
| `.env.example` | Full | Placeholders only; documents C1's key coupling |
| `.gitignore` | Full | `.env` ignored (verified never tracked) |
| `.env` (local) | Config | Key **names** + `REQUIRE_VERIFICATION=true` inspected; values NOT read into report; Turnstile keys absent (M2) |

## Scripts / seed / evals (Swept)

| File | Disposition | Notes |
|---|---|---|
| `scripts/push_fly_secrets.py` | Full | L6 (argv exposure); list-argv (no shell inj); prints key names only |
| `scripts/outbound_call.py` | Full | Manual outbound-call demo; not an app endpoint |
| `scripts/rotate_keys.md` | Full | Confirms C1 coupling; unexecuted runbook |
| `scripts/kill_switch.py` | Swept | Ops toggle (DB flag + agent unbind) |
| `scripts/qa_review.py` | Swept | Reads call QA fields; no user-input path |
| `scripts/smoke_cliniko.py` | Swept | Cliniko connectivity check |
| `scripts/setup_twilio_verify.py` | Swept | One-time Twilio Verify service setup |
| `scripts/import_twilio_number.py` | Swept | One-time number import; prints number (not secret) |
| `scripts/dump_calls.py`, `scripts/dump_transcript.py` | Swept | Diagnostics; read-only |
| `scripts/hardening_runbook.md`, `scripts/ops_runbook.md`, `scripts/live_call_checklist.md` | Full | Ops docs; align with findings |
| `seed/arogya_data.py` | Full | Demo data; DEMO_PATIENTS on dev prefix (fictional) |
| `seed/cliniko_seed.py`, `seed/local_seed.py`, `seed/ci_seed.py` | Swept | Seeders; bound-param SQL, no injection |
| `evals/*.py` | Swept | Eval harness; calls tools with shared secret (not deployed); no request-path exposure |
| `evals/results/*`, `evals/out/*` | Gen | Committed eval reports (no secrets); `evals/out` gitignored |

## Tests (Full — reviewed to align regression style)

| File | Disposition | Notes |
|---|---|---|
| `tests/test_verification.py` | Full | Proves phone-scoping / no cross-call inheritance |
| `tests/test_units_and_endpoints.py` | Full | Auth-rejection, idempotency, webhook idempotency, availability subtraction |
| `tests/test_db_guarantees.py` | Swept | (existing) exclusion-constraint race, referenced not re-run |
| `tests/test_guard.py`, `tests/test_names.py`, `tests/test_reconcile.py`, `tests/test_transfer.py` | Swept | Existing unit coverage |
| `tests/test_security_regressions.py` | **New** | Delivered by this review (see REVIEW.md §Security Regression Suite) |

## Docs / non-product (Config)

| File | Disposition | Notes |
|---|---|---|
| `README.md`, `SETUP_CLINIKO.md`, `DISCLAIMER.md`, `adr/0001-stack-choice.md` | Config | Builder claims — treated as assertions, independently verified; not part of attack surface |
| `../SECURITY_REVIEW_ADDENDUM.md`, `../Research/`, assignment docs | Excluded | Outside the repo, per scope; read for context only |

## Verification actions performed
- `git rev-list --count HEAD` = 34; `git ls-files | grep .env` = none; `git log --all -p`
  grep for `key_…`/`sk-…`/Twilio/Telegram/Neon/private-key patterns = 0 matches.
- `git grep` for `subprocess|os.system|shell=True|eval(|exec(` across app/scripts/evals/seed
  = only `push_fly_secrets.py` (list-argv, safe).
- `git grep` for f-string/`.format`/`%` SQL interpolation = 0 (all SQL uses bound params).
- Live `.env`: confirmed `REQUIRE_VERIFICATION=true`; Turnstile + MAX_WEB_CALLS keys absent.

## Not performed (with justification)
- No live calls to `+1 (628) 356-4436` or the Fly backend (may receive real evaluator
  calls; no load/stress permitted).
- Did not run `pytest`/evals (hit live Neon; eval harness mutates the real Cliniko
  calendar). New regression tests are written for CI's throwaway Postgres.
- Did not read secret **values** from `.env` into this report (only key names + the
  one boolean needed for M3).
