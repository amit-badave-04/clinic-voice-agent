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

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CallLog
from app.services import timeutils

log = logging.getLogger("guard")
settings = get_settings()

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


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
    """True when the Turnstile token is valid — or when Turnstile is not
    configured (local dev), which is logged so it can't silently stay off."""
    if not settings.turnstile_secret_key:
        log.info("turnstile not configured — skipping check")
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
