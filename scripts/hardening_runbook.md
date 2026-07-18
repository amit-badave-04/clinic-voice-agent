# Abuse-resistance runbook

What protects the system, where each control lives, and the manual (console)
steps that code cannot do. Threat model: the phone number and demo page are
public and attached to per-minute-billed AI; the realistic risks are cost
drain, bot traffic, toll fraud on the Twilio trunk, and callers social-
engineering the agent.

## Controls in code (already active after deploy)

| Control | Where | Behavior |
|---|---|---|
| Kill switch | `scripts/kill_switch.py` on/off/status | DB flag stops web-call minting + inbound context; **also unbinds the number's agent — the only way Retell actually declines PSTN calls**. `status` warns about half-flipped states. Emergency alternative: `flyctl secrets set KILL_SWITCH=true` (web channel + context only). |
| Daily web-call cap | `MAX_WEB_CALLS_PER_DAY` (default 60) | Browser calls are free for callers and cost Retell credit; over the cap, minting returns 503 until clinic-local midnight. PSTN is uncapped here — calling costs the caller money; trunk abuse is handled Twilio-side. |
| Turnstile bot gate | `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | When set, web-call minting and OTP sending require a valid Turnstile token (server-side siteverify). Unset = skipped and logged (local dev). |
| Demo identity | `/demo-personas` + OTP | The free-form caller-ID field is gone: fictional allowlisted personas (server-side number mapping, page shows the dev OTP so visitors can experience in-call verification), or the visitor's own number proven by SMS OTP before the call — which then starts pre-verified. |
| Per-IP rate limit | `app/web/router.py` | 6 mint/OTP requests per 10 minutes per IP (behind Cloudflare, uses `CF-Connecting-IP`). |
| Call cost ceilings | agent config (v13+) | Max call duration 15 min; auto-hangup after 2 min of silence. |
| Webhook hygiene | `app/retell/security.py` | 512 KB body cap before HMAC verification; signature required; mutating tools are idempotency-keyed (Retell's signature carries no timestamp, so replay safety lives there). |
| OTP discipline | `app/services/verification.py` | Codes go only to the number on file / the number being proven — never a conversation-supplied target; 3 challenges per call, 3 attempts per challenge, audit ledger. |

## Manual steps (Twilio console) — do once

1. **Voice Geo Permissions**: Console → Voice → Settings → Geo Permissions →
   disable every country except United States (the trunk's origination) and
   India. This is the standard IRSF (premium-rate fraud) mitigation: a stolen
   credential can't dial expensive destinations.
2. **Trunk security**: Elastic SIP Trunking → clinic-voice-agent-trunk →
   Termination: confirm the credential list is attached and no `0.0.0.0/0`
   IP ACL exists. Origination already points only at Retell.
3. **Usage triggers**: Console → Monitor → Usage triggers → add a daily spend
   alert (e.g. $5/day) to email. (Full alerting stack lands in the ops
   milestone.)

## Manual steps (Cloudflare) — optional, free tier

1. Add a domain (or use a workers.dev proxy) in front of
   `clinic-voice-agent.fly.dev`; proxied DNS gives the free managed WAF and
   Bot Fight Mode on the demo page.
2. Turnstile: Dashboard → Turnstile → create a widget for the demo hostname →
   set `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` in `.env`, push with
   `python -m scripts.push_fly_secrets --only TURNSTILE_SITE_KEY,TURNSTILE_SECRET_KEY`.
   The page renders the widget automatically once the site key is present.
3. If proxying through Cloudflare, keep the Fly hostname itself unadvertised;
   the app already prefers `CF-Connecting-IP` for rate limiting.

## Known residual risks (deliberate, documented)

- PSTN caller ID remains spoofable — that is why disclosure/changes require
  the in-call OTP (see README identity section), not why calls are blocked.
- The dev OTP for fictional demo personas is public by design; those patients
  hold no real data and exist to demonstrate the verification flow.
- Retell bills post-usage (no prepaid hard stop) and Fly has no billing cap —
  the kill switch plus duration caps plus the daily web cap are the spend
  containment; automated spend alerts arrive with the ops milestone.
- No WAF custom rules / IP allowlisting of Retell egress: Retell publishes a
  webhook origin IP but not a complete egress range for tool calls, so HMAC
  remains the trust anchor.
