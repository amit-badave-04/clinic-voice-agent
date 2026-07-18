"""Call-session state: powers dropped-call resume, callback recognition, and
the pre-answer context injection (dynamic variables) on inbound calls."""
import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CallSession, Patient, PendingCallback
from app.services import booking, timeutils

log = logging.getLogger("sessions")
settings = get_settings()


async def upsert_session(
    session: AsyncSession,
    call_id: str,
    phone_e164: str,
    stage: str | None = None,
    collected: dict | None = None,
) -> None:
    """Tool endpoints update the session incrementally so state survives a drop
    even if the call_ended webhook is delayed.

    Single atomic upsert: parallel tool calls (observed live — the agent issues
    simultaneous searches) must not race a SELECT-then-INSERT into a unique
    violation. 'completed' is terminal and never downgraded."""
    await session.execute(
        text(
            "INSERT INTO call_sessions (id, call_id, phone_e164, stage, collected) "
            "VALUES (:id, :call_id, :phone, COALESCE(:stage, 'started'), CAST(:collected AS jsonb)) "
            "ON CONFLICT (call_id) DO UPDATE SET "
            "  collected = call_sessions.collected || EXCLUDED.collected, "
            "  stage = CASE WHEN call_sessions.stage = 'completed' THEN 'completed' "
            "               ELSE COALESCE(:stage, call_sessions.stage) END, "
            "  phone_e164 = CASE WHEN call_sessions.phone_e164 = '' THEN EXCLUDED.phone_e164 "
            "               ELSE call_sessions.phone_e164 END, "
            "  updated_at = now()"
        ),
        {
            "id": uuid.uuid4(),
            "call_id": call_id,
            "phone": phone_e164 or "",
            "stage": stage,
            "collected": json.dumps(collected or {}),
        },
    )


async def resumable_session(session: AsyncSession, phone_e164: str) -> CallSession | None:
    """Most recent unexpired, incomplete session for this phone number."""
    rows = (
        await session.execute(
            select(CallSession)
            .where(
                CallSession.phone_e164 == phone_e164,
                CallSession.stage != "completed",
                CallSession.expires_at > timeutils.now_utc(),
            )
            .order_by(CallSession.updated_at.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def mark_session_ended(
    session: AsyncSession,
    call_id: str,
    disconnect_reason: str | None,
    summary: str,
    completed: bool,
) -> None:
    expires = timeutils.now_utc() + timedelta(minutes=settings.session_resume_ttl_minutes)
    await session.execute(
        text(
            "UPDATE call_sessions SET summary = :summary, last_disconnect_reason = :reason, "
            "stage = :stage, expires_at = :expires, updated_at = now() WHERE call_id = :call_id"
        ),
        {
            "summary": summary,
            "reason": disconnect_reason,
            "stage": "completed" if completed else "in_task",
            "expires": None if completed else expires,
            "call_id": call_id,
        },
    )
    if completed:
        # The caller's task is done — any older dangling sessions for this
        # phone are obsolete; expire them so they can't resurface as resumes.
        await session.execute(
            text(
                "UPDATE call_sessions SET expires_at = now() "
                "WHERE phone_e164 = (SELECT phone_e164 FROM call_sessions WHERE call_id = :call_id) "
                "AND call_id != :call_id AND stage != 'completed'"
            ),
            {"call_id": call_id},
        )


async def owed_callback(session: AsyncSession, phone_e164: str) -> PendingCallback | None:
    rows = (
        await session.execute(
            select(PendingCallback)
            .where(PendingCallback.phone_e164 == phone_e164, PendingCallback.owed.is_(True))
            .order_by(PendingCallback.created_at.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def build_inbound_context(session: AsyncSession, phone_e164: str) -> dict:
    """Everything the agent should know BEFORE it says hello.
    Returned as Retell dynamic variables (string values only)."""
    variables: dict[str, str] = {
        "current_datetime_ist": timeutils.current_datetime_prompt_string(),
        # Destination for the built-in warm-transfer tool — backend config,
        # never conversation input. resolve_live_transfer gates its use.
        "transfer_number": settings.staff_transfer_target,
        "caller_phone": phone_e164 or "unknown",
        "known_patient": "false",
        "patient_names": "",
        "multiple_patients": "false",
        "upcoming_appointments": "none",
        "resume_context": "none",
        "owed_callback_context": "none",
        "last_interaction": "none",
    }
    if not phone_e164:
        return variables

    # Most recent COMPLETED call's summary (24h window) — continuity context so
    # the agent never denies a previous call happened. Distinct from
    # resume_context, which is only for interrupted calls.
    last = (
        await session.execute(
            text(
                "SELECT summary, ended_at FROM call_log "
                "WHERE phone_e164 = :phone AND summary != '' "
                "AND ended_at > now() - interval '24 hours' "
                "ORDER BY ended_at DESC LIMIT 1"
            ),
            {"phone": phone_e164},
        )
    ).first()
    if last:
        when_local = timeutils.format_local(last.ended_at, "%I:%M %p")
        variables["last_interaction"] = f"(earlier call, ended around {when_local}) {last.summary}"

    patients = (
        (await session.execute(select(Patient).where(Patient.phone_e164 == phone_e164)))
        .scalars()
        .all()
    )
    if patients:
        variables["known_patient"] = "true"
        variables["patient_names"] = ", ".join(p.full_name for p in patients)
        variables["multiple_patients"] = "true" if len(patients) > 1 else "false"
        upcoming = await booking.upcoming_appointments_for_phone(session, phone_e164)
        if upcoming:
            variables["upcoming_appointments"] = "; ".join(
                f"{a['patient_name']}: {a['appointment_type']} with {a['practitioner']} "
                f"at {a['branch']} on {a['when']} (appointment_id: {a['appointment_id']})"
                for a in upcoming[:3]
            )

    resumable = await resumable_session(session, phone_e164)
    if resumable and (resumable.summary or resumable.collected):
        details = resumable.summary or ""
        if resumable.collected:
            details += " Collected so far: " + ", ".join(
                f"{k}={v}" for k, v in resumable.collected.items()
            )
        variables["resume_context"] = details.strip()
        # NOT consumed here: injection happens pre-answer, and a call that
        # never connects (observed: error_user_not_joined) would destroy the
        # context. Consumption happens in the call_started webhook —
        # see consume_injected_context().

    callback = await owed_callback(session, phone_e164)
    if callback:
        variables["owed_callback_context"] = callback.context_summary
        # Consumed in consume_injected_context() once the call actually connects.

    return variables


async def consume_injected_context(session: AsyncSession, phone_e164: str, call_id: str) -> None:
    """One-shot contexts (dropped-call resume, owed callbacks) are consumed when
    a call CONNECTS, not when the pre-answer webhook fires — so a failed
    connection can't destroy them, and every connected call delivers each
    exactly once."""
    if not phone_e164:
        return
    await session.execute(
        text(
            "UPDATE call_sessions SET expires_at = now() "
            "WHERE phone_e164 = :phone AND call_id != :call_id "
            "AND stage != 'completed' AND expires_at > now()"
        ),
        {"phone": phone_e164, "call_id": call_id},
    )
    await session.execute(
        text("UPDATE pending_callbacks SET owed = false WHERE phone_e164 = :phone AND owed"),
        {"phone": phone_e164},
    )
