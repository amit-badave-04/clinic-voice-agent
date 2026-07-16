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
        variables = await sessions_svc.build_inbound_context(session, phone)
        await session.commit()

    return {"call_inbound": {"dynamic_variables": variables}}


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


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
    return {"ok": True}


async def _on_call_started(call: dict) -> None:
    call_id = call["call_id"]
    phone = normalize_phone(call.get("from_number"))
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
        await session.commit()


async def _on_call_ended(call: dict) -> None:
    call_id = call["call_id"]
    phone = normalize_phone(call.get("from_number"))
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
