"""Admission control: kill switch, web-call volume cap, Turnstile verification.

Layers (each independent; see scripts/hardening_runbook.md for operations):
  - Kill switch: clinic_policies row 'kill_switch' (set by scripts/kill_switch.py,
    which also unbinds the phone number's agent — the only way a PSTN call is
    actually rejected) or the KILL_SWITCH env flag. Checked before web-call
    minting and inbound context injection.
  - Daily web-call cap: browser calls are free for the caller and cost real
    Retell credit, so they get a hard daily ceiling. PSTN needs no cap here —
    calling costs the caller money; telephony-side abuse is Twilio's
    geo-permissions job.
  - Turnstile: bot gate on token minting, active only when keys are configured.
"""
import logging
import time as _time
from collections import defaultdict, deque

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CallLog
from app.services import timeutils

log = logging.getLogger("guard")
settings = get_settings()

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# ── In-process sliding-window rate limiter ──────────────────────────────────
# The deployment is deliberately a single always-warm machine
# (fly.toml min_machines_running=1, no HA), so process-local counters are the
# correct scope for per-call / per-phone / per-IP abuse control. Global daily
# ceilings that must survive a restart live in the DB instead (see
# verification.start_challenge and booking day-cap).
_RATE_MAX_KEYS = 20_000
_buckets: dict[str, deque] = defaultdict(deque)


def rate_ok(key: str, max_events: int, window_seconds: int) -> bool:
    """True (and records the event) when `key` is under `max_events` in the
    trailing `window_seconds`; False when the limit is already reached.
    max_events <= 0 disables the limit (always True)."""
    if max_events <= 0:
        return True
    now = _time.monotonic()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= max_events:
        return False
    bucket.append(now)
    if len(_buckets) > _RATE_MAX_KEYS:  # bound memory over long uptime
        _evict_empty_buckets()
    return True


def _evict_empty_buckets() -> None:
    for key in [k for k, b in list(_buckets.items()) if not b]:
        _buckets.pop(key, None)


async def kill_switch_on(session: AsyncSession) -> bool:
    if settings.kill_switch:
        return True
    row = (
        await session.execute(
            text("SELECT value FROM clinic_policies WHERE key = 'kill_switch'")
        )
    ).first()
    return bool(row and row.value == "on")


async def web_calls_today(session: AsyncSession) -> int:
    """Web calls registered since clinic-local midnight."""
    midnight_local = timeutils.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        await session.execute(
            select(func.count())
            .select_from(CallLog)
            .where(CallLog.direction == "web", CallLog.created_at >= midnight_local)
        )
    ).scalar_one()


async def web_channel_open(session: AsyncSession) -> tuple[bool, str]:
    if await kill_switch_on(session):
        return False, "The demo is temporarily paused."
    if settings.max_web_calls_per_day:
        count = await web_calls_today(session)
        if count >= settings.max_web_calls_per_day:
            log.warning("daily web-call cap reached (%s)", count)
            return False, "Today's demo call limit has been reached — please try again tomorrow."
    return True, ""


async def verify_turnstile(token: str | None, client_ip: str) -> bool:
    """True when the Turnstile token is valid. When Turnstile is not configured,
    fail CLOSED in production (a bot gate that silently disables itself is not a
    control) and open only in an explicit non-production environment."""
    if not settings.turnstile_secret_key:
        if settings.is_production:
            log.warning("turnstile secret not configured in production — failing closed")
            return False
        log.info("turnstile not configured (non-production) — skipping check")
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": settings.turnstile_secret_key,
                    "response": token,
                    "remoteip": client_ip,
                },
            )
        return response.status_code == 200 and response.json().get("success") is True
    except httpx.HTTPError as exc:
        log.warning("turnstile verify failed: %s", exc)
        return False
