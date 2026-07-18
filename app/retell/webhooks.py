"""Retell webhooks.

POST /retell/inbound  — call_inbound (pre-answer, 10s budget): look up the caller
                        and answer with dynamic variables so the agent already
                        knows the patient, their upcoming appointments, any
                        dropped-call context and any owed callback BEFORE hello.
POST /retell/webhook  — call_started / call_ended / call_analyzed. Retries up to
                        3x, so handlers are idempotent on (event, call_id).
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.db.session import SessionLocal
from app.retell.security import verify_retell_request
from app.services import guard
from app.services import sessions as sessions_svc
from app.services import summarize
from app.services.phone import normalize_phone

log = logging.getLogger("retell.webhooks")
router = APIRouter()

# Outbound-call outcomes that mean "we owe this patient a context-carrying callback"
NO_ANSWER_REASONS = {
    "dial_no_answer",
    "dial_busy",
    "dial_failed",
    "voicemail_reached",
    "machine_detected",
}


@router.post("/inbound")
async def call_inbound(request: Request) -> dict:
    raw = await verify_retell_request(request)
    payload = json.loads(raw)
    info = payload.get("call_inbound", {})
    phone = normalize_phone(info.get("from_number"))
    log.info("inbound call from %s", phone)

    async with SessionLocal() as session:
        if await guard.kill_switch_on(session):
            # A bare 200 declines the call outright once the number's agent
            # binding is removed (scripts/kill_switch.py does both); with an
            # agent still bound the call proceeds context-free — degraded, not
            # dangerous.
            log.warning("kill switch on — declining inbound from %s", phone)
            return {"call_inbound": {}}
        variables = await sessions_svc.build_inbound_context(session, phone)
        await session.commit()

    return {"call_inbound": {"dynamic_variables": variables}}


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def _call_phone(call: dict) -> str:
    """Caller identity: PSTN caller ID, else the web page's simulated caller ID.
    Without the metadata fallback, web-call sessions get keyed to an empty
    phone and dropped-call resume silently breaks for web callers."""
    metadata = call.get("metadata") or {}
    return normalize_phone(call.get("from_number") or metadata.get("simulated_phone"))


@router.post("/webhook")
async def retell_webhook(request: Request) -> dict:
    raw = await verify_retell_request(request)
    payload = json.loads(raw)
    event = payload.get("event")
    call = payload.get("call", {})
    call_id = call.get("call_id", "")
    if not call_id:
        return {"ok": True}

    if event == "call_started":
        await _on_call_started(call)
    elif event == "call_ended":
        await _on_call_ended(call)
    elif event == "call_analyzed":
        await _on_call_analyzed(call)
    elif event in ("transfer_started", "transfer_bridged", "transfer_cancelled", "transfer_ended"):
        await _on_transfer_event(event, call_id)
    return {"ok": True}


# Retell transfer webhook events → ticket lifecycle. A cancelled transfer
# means the human leg never picked up; the agent's fallback then logs the
# callback ticket, and this one records the failed attempt.
_TRANSFER_STATUS = {
    "transfer_started": "transfer_started",
    "transfer_bridged": "transfer_bridged",
    "transfer_cancelled": "transfer_failed",
    "transfer_ended": "transfer_completed",
}


async def _on_transfer_event(event: str, call_id: str) -> None:
    from app.services import alerts, transfer

    async with SessionLocal() as session:
        await transfer.update_ticket_status(session, call_id, _TRANSFER_STATUS[event])
        await session.commit()
    log.info("transfer event %s for %s", event, call_id)
    if event == "transfer_cancelled":
        alerts.notify_bg(f"❌ Warm transfer for {call_id} was not answered — caller gets a callback promise")


async def _on_call_started(call: dict) -> None:
    call_id = call["call_id"]
    phone = _call_phone(call)
    direction = call.get("direction") or ("web" if call.get("call_type") == "web_call" else "inbound")
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO call_log (call_id, phone_e164, direction, status, started_at) "
                "VALUES (:id, :phone, :dir, 'started', now()) "
                "ON CONFLICT (call_id) DO NOTHING"
            ),
            {"id": call_id, "phone": phone or None, "dir": direction},
        )
        await sessions_svc.upsert_session(session, call_id, phone, stage="started")
        # The call is now genuinely connected — consume any one-shot context
        # (resume / owed callback) that the inbound webhook injected.
        await sessions_svc.consume_injected_context(session, phone, call_id)
        await session.commit()


async def _on_call_ended(call: dict) -> None:
    call_id = call["call_id"]
    phone = _call_phone(call)
    direction = call.get("direction", "inbound")
    reason = call.get("disconnection_reason", "")
    transcript = call.get("transcript", "")

    async with SessionLocal() as session:
        # Idempotency: (event, call_id) — skip if this call is already marked ended.
        existing = (
            await session.execute(
                text("SELECT status FROM call_log WHERE call_id = :id"), {"id": call_id}
            )
        ).first()
        if existing and existing.status == "ended":
            return
        await session.execute(
            text(
                "INSERT INTO call_log (call_id, phone_e164, direction, status, disconnection_reason, ended_at) "
                "VALUES (:id, :phone, :dir, 'ended', :reason, now()) "
                "ON CONFLICT (call_id) DO UPDATE SET status = 'ended', "
                "disconnection_reason = :reason, ended_at = now()"
            ),
            {"id": call_id, "phone": phone or None, "dir": direction, "reason": reason},
        )

        # Missed OUTBOUND call -> owe a callback that carries the original context.
        if direction == "outbound" and reason in NO_ANSWER_REASONS:
            context = (call.get("metadata") or {}).get("callback_context", "We called to follow up.")
            await session.execute(
                text(
                    "INSERT INTO pending_callbacks (id, phone_e164, context_summary) "
                    "VALUES (gen_random_uuid(), :phone, :ctx)"
                ),
                {"phone": normalize_phone(call.get("to_number")), "ctx": context},
            )
            await session.commit()
            return

        # Dropped/incomplete INBOUND call -> summarize for resume on ring-back.
        row = (
            await session.execute(
                text("SELECT stage, collected FROM call_sessions WHERE call_id = :id"),
                {"id": call_id},
            )
        ).first()
        stage = row.stage if row else "started"
        collected = row.collected if row else {}
        completed = stage == "completed"
        summary = ""
        if not completed:
            summary = await summarize.summarize_incomplete_call(transcript, collected or {})
        await sessions_svc.mark_session_ended(session, call_id, reason, summary, completed)
        await session.commit()


async def _on_call_analyzed(call: dict) -> None:
    call_id = call["call_id"]
    analysis = call.get("call_analysis", {}) or {}
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE call_log SET summary = :summary, raw = CAST(:raw AS jsonb) WHERE call_id = :id"
            ),
            {
                "summary": analysis.get("call_summary", "") or "",
                "raw": json.dumps({"call_analysis": analysis}),
                "id": call_id,
            },
        )
        await session.commit()
