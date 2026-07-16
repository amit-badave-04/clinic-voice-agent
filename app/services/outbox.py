"""Transactional-outbox: the ONLY path that writes appointments to Cliniko.

Design:
  - Booking/reschedule/cancel commit locally AND enqueue an outbox event in the
    same transaction (atomic intent). No HTTP ever runs inside that transaction
    — external calls inside an open transaction held exclusion-constraint locks
    and blocked concurrent bookings for the same practitioner.
  - The caller then drains its own event inline via process_event_inline()
    (fast path, gives the agent a truthful pms_sync answer).
  - The background worker retries anything the inline attempt missed, with
    exponential backoff and FOR UPDATE SKIP LOCKED so the two never collide.
  - Events carry only {"appointment_id"}: the worker derives the CURRENT state
    from the database, so a reschedule or cancellation that lands before the
    original create has synced is handled correctly (create uses the new time;
    a cancelled-before-sync appointment is never created in Cliniko at all).
"""
import asyncio
import logging

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.cliniko import get_cliniko

log = logging.getLogger("outbox")

POLL_SECONDS = 5
MAX_ATTEMPTS = 10


async def _load_appointment(session, appointment_id: str):
    return (
        await session.execute(
            text(
                "SELECT a.id, a.status, a.cliniko_appointment_id, "
                "       lower(a.during) AS starts_at, upper(a.during) AS ends_at, "
                "       p.id AS patient_id, p.full_name, p.phone_e164, p.cliniko_patient_id, "
                "       pr.cliniko_practitioner_id, b.cliniko_business_id, t.cliniko_appointment_type_id "
                "FROM appointments a "
                "JOIN patients p ON p.id = a.patient_id "
                "JOIN practitioners pr ON pr.id = a.practitioner_id "
                "JOIN branches b ON b.id = a.branch_id "
                "JOIN appointment_types t ON t.id = a.appointment_type_id "
                "WHERE a.id = :id"
            ),
            {"id": appointment_id},
        )
    ).first()


async def _mark_synced(session, appointment_id: str, cliniko_appointment_id: str | None = None) -> None:
    if cliniko_appointment_id:
        await session.execute(
            text(
                "UPDATE appointments SET cliniko_appointment_id = :cid, cliniko_sync_status = 'synced' "
                "WHERE id = :id"
            ),
            {"cid": cliniko_appointment_id, "id": appointment_id},
        )
    else:
        await session.execute(
            text("UPDATE appointments SET cliniko_sync_status = 'synced' WHERE id = :id"),
            {"id": appointment_id},
        )


async def _process_event(session, row) -> None:
    """Executes one event against Cliniko, deriving state from the DB."""
    cliniko = get_cliniko()
    appointment_id = row.payload["appointment_id"]
    appt = await _load_appointment(session, appointment_id)
    if appt is None:
        log.warning("outbox event %s: appointment %s no longer exists; skipping", row.id, appointment_id)
        return

    if row.event_type == "create_appointment":
        if appt.status == "cancelled" and not appt.cliniko_appointment_id:
            # Cancelled before it ever reached Cliniko: absent on both sides is
            # consistent — never create it, and mark the row settled.
            await _mark_synced(session, appointment_id)
            return
        if appt.cliniko_appointment_id:
            return  # already created (e.g. by an earlier retry)
        cliniko_patient_id = appt.cliniko_patient_id
        if not cliniko_patient_id:
            parts = appt.full_name.split()
            created = await cliniko.create_patient(parts[0], " ".join(parts[1:]) or "-", appt.phone_e164)
            cliniko_patient_id = str(created.get("id"))
            await session.execute(
                text("UPDATE patients SET cliniko_patient_id = :cid WHERE id = :id"),
                {"cid": cliniko_patient_id, "id": appt.patient_id},
            )
        created = await cliniko.create_appointment(
            appt.cliniko_appointment_type_id,
            appt.cliniko_business_id,
            cliniko_patient_id,
            appt.cliniko_practitioner_id,
            appt.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        await _mark_synced(session, appointment_id, str(created.get("id")))

    elif row.event_type == "update_appointment":
        if appt.status == "cancelled":
            return  # a cancel event will (or did) handle it
        if not appt.cliniko_appointment_id:
            return  # pending create event will use the current (new) time
        await cliniko.update_appointment(
            appt.cliniko_appointment_id,
            appt.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            appt.ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        await _mark_synced(session, appointment_id)

    elif row.event_type == "cancel_appointment":
        if not appt.cliniko_appointment_id:
            # Never reached Cliniko: absent on both sides is consistent.
            await _mark_synced(session, appointment_id)
            return
        await cliniko.cancel_appointment(appt.cliniko_appointment_id)
        await _mark_synced(session, appointment_id)

    else:
        raise ValueError(f"unknown outbox event_type {row.event_type}")


async def _claim_and_process(session, row) -> bool:
    try:
        await _process_event(session, row)
        await session.execute(
            text("UPDATE outbox SET status = 'succeeded' WHERE id = :id"), {"id": row.id}
        )
        return True
    except Exception as exc:  # noqa: BLE001 — any failure becomes a scheduled retry
        log.warning("outbox event %s failed: %s", row.id, exc)
        await session.execute(
            text(
                "UPDATE outbox SET attempts = attempts + 1, last_error = :err, "
                "status = CASE WHEN attempts + 1 >= :max THEN 'failed' ELSE 'pending' END, "
                "next_attempt_at = now() + (interval '1 second' * least(300, power(2, attempts + 1))) "
                "WHERE id = :id"
            ),
            {"err": str(exc)[:500], "max": MAX_ATTEMPTS, "id": row.id},
        )
        return False


async def process_event_inline(event_id: int) -> bool:
    """Fast-path drain of a just-enqueued event. Returns True when the event is
    (now or already) succeeded. SKIP LOCKED means a concurrent worker pass and
    this call never double-process."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, event_type, payload FROM outbox "
                    "WHERE id = :id AND status = 'pending' FOR UPDATE SKIP LOCKED"
                ),
                {"id": event_id},
            )
        ).first()
        if row is None:
            status = (
                await session.execute(text("SELECT status FROM outbox WHERE id = :id"), {"id": event_id})
            ).first()
            return bool(status and status.status == "succeeded")
        ok = await _claim_and_process(session, row)
        await session.commit()
        return ok


async def process_due_events() -> int:
    """One worker pass; returns number of rows processed. Exposed for tests."""
    processed = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, event_type, payload FROM outbox "
                    "WHERE status = 'pending' AND next_attempt_at <= now() "
                    "ORDER BY id LIMIT 10 FOR UPDATE SKIP LOCKED"
                )
            )
        ).all()
        for row in rows:
            await _claim_and_process(session, row)
            processed += 1
        await session.commit()
    return processed


async def outbox_worker_loop() -> None:
    while True:
        try:
            await process_due_events()
        except Exception as exc:  # noqa: BLE001 — worker must never die
            log.warning("outbox pass failed: %s", exc)
        await asyncio.sleep(POLL_SECONDS)
