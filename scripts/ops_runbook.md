# Operations runbook (solo-operator scale)

What pages, what merely records, and the one-time account setups. Principle:
every alert-worthy event is durably in Postgres first (tickets, outbox status,
reconcile results) — notifications are the pager, never the record.

## Alert wiring (in code, settings-gated)

| Event | Where it fires | Sink |
|---|---|---|
| Human callback owed | `log_followup_request` tool | Telegram + Slack, immediate |
| PMS write-back permanently failed (10 retries) | outbox worker | Telegram + Slack, immediate |
| Calendar drift found/healed | reconcile cycle summary | Telegram + Slack |
| Flagged calls digest | `python -m scripts.qa_review` (run ad hoc / cron) | Telegram + Slack |

No sink configured → alerts are log lines only (`alerts.py` logs this), nothing breaks.

## One-time setups (each optional, all free tiers)

1. **Telegram** (recommended primary — instant push on your phone): message
   @BotFather → `/newbot` → put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
   Message your new bot once, then get your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → `TELEGRAM_CHAT_ID`.
   Push both: `python -m scripts.push_fly_secrets --only TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID`.
2. **Slack** (alternative/additional): create an incoming webhook →
   `SLACK_WEBHOOK_URL`.
3. **Sentry** (backend exceptions + traces): create a free project → set
   `SENTRY_DSN` and push. FastAPI auto-instruments on boot.
4. **UptimeRobot** (external uptime): monitor
   `https://clinic-voice-agent.fly.dev/healthz`, alert after 2 consecutive
   failures. `/healthz` probes the database, so this also catches a wedged Neon.
5. **Healthchecks.io** (watchdog for the reconcile loop): create a check with
   period = 30 min, grace = 15 min → set `HEALTHCHECKS_RECONCILE_URL`. The loop
   pings after every successful cycle; a dead loop misses pings and pages.
6. **Twilio usage trigger**: Console → Monitor → Usage triggers → daily spend
   alert (e.g. $5). Twilio spend is invisible to Retell's dashboards.
7. **Retell native alerting**: Dashboard → Alerting — add "Custom Function
   Failure Count > 3 in 5m" and "Call Success Rate < 85% in 30m" on the
   production agent. (Retell incidents fire once, no auto-resolve notice.)

## Cliniko drift reconciliation (automatic)

Every 30 min (`RECONCILE_INTERVAL_MINUTES`) the app compares local Postgres
against Cliniko over a [-1d, +60d] window:

- **Staff created an appointment in Cliniko** → mirrored locally
  (`source_system='cliniko'`) so the no-double-booking constraint and the
  agent see it.
- **Staff moved an agent-booked appointment** → local time updated
  (`externally_modified=true`); if the new time collides locally → ticket.
- **Local confirmed but cancelled/absent in Cliniko** → never auto-cancelled:
  a `followup_tickets` row (phone `reconcile`) asks a human to confirm with
  the patient first.
- Rows still pending sync are the outbox's job, not reconciliation's.

Demo-scale note: each cycle is a full-window comparison (dozens of rows). At
production volume, switch to incremental `q[]=updated_at:>` pulls + a nightly
full sweep — the bucket logic stays identical.

## Degradation behavior (what the caller hears)

| Failure | Behavior |
|---|---|
| Cliniko down mid-call | Booking/change commits locally + outbox retries; the tool response carries a `sync_note` and the agent presents it as **reserved, clinic will confirm shortly** — never an unqualified "confirmed". |
| Backend down | Retell tool call times out (6–12s caps); agent apologizes per prompt rule 10 and offers a callback. Fly restarts unhealthy machines via `/healthz`. |
| TTS provider outage | Agent config carries a cross-provider `fallback_voice_ids`; Retell fails over mid-call for the rest of the call. |
| SMS/OTP undeliverable | Verification tools return explicit statuses; agent offers a staff callback — never bypasses verification. |
