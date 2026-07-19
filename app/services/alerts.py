"""Operator notifications for a solo-maintained system.

One function, two sinks (Telegram bot + Slack incoming webhook), both optional
and settings-gated. Alerts are best-effort by design: they run detached with a
short timeout and never raise — a Telegram outage must not take down a live
call's tool path. Anything alerted here is ALSO durably in the database
(followup_tickets, outbox.status, reconcile tickets); the alert is the pager,
not the record.
"""
import asyncio
import logging
import re

import httpx

from app.config import get_settings

log = logging.getLogger("alerts")
settings = get_settings()

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(message: str) -> str:
    """Alert bodies embed caller-controlled text (names, reasons, phones). Strip
    control characters so a caller cannot forge log/pager structure or inject
    newlines into the operator's notification (N2)."""
    return _CONTROL_CHARS.sub(" ", message or "")[:4000]


async def notify(message: str) -> None:
    """Send to every configured sink; log when none is configured."""
    message = _sanitize(message)
    sinks = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        sinks.append(_telegram(message))
    if settings.slack_webhook_url:
        sinks.append(_slack(message))
    if not sinks:
        log.info("alert (no sink configured): %s", message)
        return
    await asyncio.gather(*sinks, return_exceptions=True)


def notify_bg(message: str) -> None:
    """Fire-and-forget from request/worker paths — never blocks, never raises."""
    try:
        asyncio.get_running_loop().create_task(notify(message))
    except RuntimeError:  # no running loop (sync script context)
        asyncio.run(notify(message))


async def _telegram(message: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": message[:4000]},
            )
    except httpx.HTTPError as exc:
        log.warning("telegram alert failed: %s", exc)


async def _slack(message: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            await client.post(settings.slack_webhook_url, json={"text": message[:4000]})
    except httpx.HTTPError as exc:
        log.warning("slack alert failed: %s", exc)
