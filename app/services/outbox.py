"""Transactional-outbox worker: retries Cliniko write-backs with exponential
backoff. Local Postgres is the source of truth; Cliniko is eventually
reconciled. Rows are claimed with FOR UPDATE SKIP LOCKED so multiple workers
never double-process."""
import asyncio
import logging

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.cliniko import get_cliniko

log = logging.getLogger("outbox")

POLL_SECONDS = 5
MAX_ATTEMPTS = 10


async def _process_event(session, row) -> None:
    payload = row.payload
    cliniko = get_cliniko()

    if row.event_type == "create_appointment":
        # Ensure the Cliniko patient exists first (it may have failed at booking time).
        patient = (
            await session.execute(
                text("SELECT id, full_name, phone_e164, cliniko_patient_id FROM patients WHERE id = :id"),
                {"id": payload["patient_id"]},
            )
        ).first()
        cliniko_patient_id = patient.cliniko_patient_id if patient else None
        if patient and not cliniko_patient_id:
            parts = patient.full_name.split()
            created = await cliniko.create_patient(parts[0], " ".join(parts[1:]) or "-", patient.phone_e164)
            cliniko_patient_id = str(created.get("id"))
            await session.execute(
                text("UPDATE patients SET cliniko_patient_id = :cid WHERE id = :id"),
                {"cid": cliniko_patient_id, "id": payload["patient_id"]},
            )
        created = await cliniko.create_appointment(
            payload["cliniko_appointment_type_id"],
            payload["cliniko_business_id"],
            cliniko_patient_id,
            payload["cliniko_practitioner_id"],
            payload["starts_at"],
        )
        await session.execute(
            text(
                "UPDATE appointments SET cliniko_appointment_id = :cid, cliniko_sync_status = 'synced' "
                "WHERE id = :id"
            ),
            {"cid": str(created.get("id")), "id": payload["appointment_id"]},
        )

    elif row.event_type == "update_appointment":
        await cliniko.update_appointment(payload["cliniko_appointment_id"], payload["starts_at"])
        await session.execute(
            text("UPDATE appointments SET cliniko_sync_status = 'synced' WHERE id = :id"),
            {"id": payload["appointment_id"]},
        )

    elif row.event_type == "cancel_appointment":
        await cliniko.cancel_appointment(payload["cliniko_appointment_id"])
        await session.execute(
            text("UPDATE appointments SET cliniko_sync_status = 'synced' WHERE id = :id"),
            {"id": payload["appointment_id"]},
        )
    else:
        raise ValueError(f"unknown outbox event_type {row.event_type}")


async def process_due_events() -> int:
    """One pass; returns number of rows processed. Exposed for tests."""
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
            try:
                await _process_event(session, row)
                await session.execute(
                    text("UPDATE outbox SET status = 'succeeded' WHERE id = :id"), {"id": row.id}
                )
            except Exception as exc:  # noqa: BLE001
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
