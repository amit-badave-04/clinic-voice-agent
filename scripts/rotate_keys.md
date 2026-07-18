# Credential rotation runbook

Rotate every credential the app uses, one integration at a time, with zero downtime.
Principle (standard practice): **create the new credential first → cut over → verify →
revoke the old one.** Never delete a working key before its replacement is proven.

Secrets never pass through chat tools or shell history: edit `.env` locally, then push
all values to Fly with `python -m scripts.push_fly_secrets` (reads `.env` directly).

## Order of operations

Each step: create new → update `.env` → `python -m scripts.push_fly_secrets --only <KEYS>`
(this restarts the app) → run the verification listed → revoke old.

1. **OPENAI_API_KEY** — platform.openai.com → create a new *project-scoped* key
   (restricted: Model capabilities only). Verify: `python -m scripts.smoke_cliniko`
   isn't needed here; instead make one browser test call (post-call summary uses this
   key) or run a single eval scenario. Revoke old key.
2. **CLINIKO_API_KEY** — Cliniko → My Info → regenerate API key (per-user key inherits
   that user's role). Verify: `python -m scripts.smoke_cliniko`. Old key dies on
   regeneration (Cliniko replaces it) — do this cutover promptly.
3. **DATABASE_URL / DATABASE_URL_DIRECT** — Neon console → reset password for the app
   role; update both URLs in `.env`. Verify: `https://<app>/healthz` returns 200
   (it probes the DB). Note: local network sometimes can't reach Neon — trust the
   deployed `/healthz`, not local psql.
4. **TWILIO_AUTH_TOKEN** — Twilio Console → Account → API keys & tokens → request
   secondary Auth Token, then promote it. Also regenerate the account **recovery code**.
   Verify: place one inbound phone call. (Twilio creds are only used by
   `scripts/import_twilio_number.py` at setup time, so exposure risk is account-level,
   not call-path.)
5. **RETELL_API_KEY** — Retell dashboard → API Keys → create new key. Our webhook and
   tool-call verification uses the API key as the HMAC secret
   (`app/retell/security.py`), so cut over atomically: update `.env`, push the Fly
   secret, then delete the old key immediately so Retell signs with the new one.
   Verify: browser test call end-to-end (inbound webhook + one tool call must both
   pass signature checks). If the dashboard offers a webhook-signing-key designation,
   set it to the new key explicitly.
6. **TOOL_SHARED_SECRET** — self-issued: generate a fresh value
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). This value is
   baked into the agent's tool definitions, so after pushing the secret you MUST
   re-publish the agent: `python -m agent.agent_config sync`. The eval harness sends
   it as `X-Tool-Secret`. Verify: one eval scenario + one browser test call.
7. **Optional — Twilio SIP trunk credential** (only if trunk credentials were exposed):
   re-run `python -m scripts.import_twilio_number` — `ensure_credentials` mints a fresh
   SIP credential and re-imports the number into Retell with it. Verify: inbound phone
   call rings through.

## After all rotations

- Full check: `pytest tests -q`, one phone call, one browser call.
- Confirm no old keys remain active in any vendor dashboard.
- Search local shells/notebooks/exports for stray copies of old values, then done —
  old values are dead anyway once revoked.
