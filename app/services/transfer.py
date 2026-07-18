"""Warm-transfer routing: business truth lives here, not in the prompt.

The agent asks resolve_live_transfer before ever attempting a transfer; this
module decides (hours, channel, configuration) and records the attempt as a
followup ticket whose lifecycle Retell's transfer webhooks then drive. The
prompt only carries conversation behavior; a mis-following LLM cannot invent
a transfer window or a destination.
"""
import logging
from datetime import datetime, time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import alerts, timeutils

log = logging.getLogger("transfer")
settings = get_settings()

OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 30)


def in_transfer_window(now_local: datetime | None = None) -> bool:
    """Clinic staffed hours: Mon–Sat 09:00–18:30 IST (Sunday closed)."""
    now_local = now_local or timeutils.now_local()
    return now_local.weekday() != 6 and OPEN_TIME <= now_local.time() < CLOSE_TIME


async def build_plan(
    session: AsyncSession, call_id: str, phone: str, is_phone_call: bool, reason: str
) -> dict:
    """Transfer decision + attempt ticket. The returned message tells the
    agent exactly what to do next in every branch."""
    if not settings.staff_transfer_target:
        return {
            "allow_transfer_now": False,
            "reason": "transfers_not_configured",
            "message": "Live transfer is not available. Offer a staff callback (log_followup_request).",
        }
    if not is_phone_call:
        return {
            "allow_transfer_now": False,
            "reason": "web_call",
            "message": (
                "Browser calls cannot be transferred. Offer a staff callback "
                "(log_followup_request) instead."
            ),
        }
    if not in_transfer_window():
        return {
            "allow_transfer_now": False,
            "reason": "outside_clinic_hours",
            "message": (
                "The clinic is closed right now (Mon–Sat, nine to six thirty). Tell the caller "
                "staff will ring them back when the clinic opens, and log_followup_request."
            ),
        }

    await session.execute(
        text(
            "INSERT INTO followup_tickets (id, phone_e164, patient_name, reason, urgency, call_id, status) "
            "VALUES (gen_random_uuid(), :phone, '', :reason, 'normal', :call_id, 'transfer_started')"
        ),
        {"phone": phone or "unknown", "reason": f"Live transfer: {reason[:400]}", "call_id": call_id},
    )
    # The Telegram ping doubles as the digital handoff summary — the human's
    # phone rings seconds after this lands.
    alerts.notify_bg(f"🔔 Warm transfer incoming from {phone or 'unknown'}: {reason[:200]}")
    return {
        "allow_transfer_now": True,
        "reason": "within_clinic_hours",
        "message": (
            "Transfer approved. Tell the caller you are connecting them to the front desk and "
            "staying on the line, then use the transfer_to_front_desk tool. If it fails or "
            "nobody answers, apologize and log_followup_request for a callback."
        ),
    }


async def update_ticket_status(session: AsyncSession, call_id: str, status: str) -> None:
    await session.execute(
        text(
            "UPDATE followup_tickets SET status = :status WHERE id = ("
            "SELECT id FROM followup_tickets WHERE call_id = :call_id "
            "AND status LIKE 'transfer%' ORDER BY created_at DESC LIMIT 1)"
        ),
        {"status": status, "call_id": call_id},
    )
