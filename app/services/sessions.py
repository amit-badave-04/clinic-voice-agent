"""Call-session state: powers dropped-call resume, callback recognition, and
the pre-answer context injection (dynamic variables) on inbound calls."""
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
    session: AsyncSession, call_id: str, phone_e164: str, **updates
) -> None:
    """Tool endpoints update the session incrementally so state survives a drop
    even if the call_ended webhook is delayed."""
    existing = (
        (await session.execute(select(CallSession).where(CallSession.call_id == call_id)))
        .scalars()
        .first()
    )
    if existing:
        collected = dict(existing.collected or {})
        collected.update(updates.pop("collected", {}))
        existing.collected = collected
        for key, value in updates.items():
            setattr(existing, key, value)
    else:
        session.add(
            CallSession(
                id=uuid.uuid4(),
                call_id=call_id,
                phone_e164=phone_e164,
                collected=updates.pop("collected", {}),
                **updates,
            )
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
        "caller_phone": phone_e164 or "unknown",
        "known_patient": "false",
        "patient_names": "",
        "multiple_patients": "false",
        "upcoming_appointments": "none",
        "resume_context": "none",
        "owed_callback_context": "none",
    }
    if not phone_e164:
        return variables

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
                f"at {a['branch']} on {a['when']}"
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

    callback = await owed_callback(session, phone_e164)
    if callback:
        variables["owed_callback_context"] = callback.context_summary
        callback.owed = False  # consumed — the agent will acknowledge it this call

    return variables
