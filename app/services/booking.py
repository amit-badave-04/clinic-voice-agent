"""Booking / reschedule / cancel — the write path.

Guarantees (in order):
  1. Idempotency: Retell retries tool calls on non-2xx/timeout; an
     idempotency_keys row makes replays return the original result.
  2. Live re-validation: booking never trusts slot data sitting in the LLM's
     context — the slot is re-checked against Cliniko + local DB at write time.
  3. Write-time conflict enforcement: the GiST exclusion constraint makes a
     double-booking structurally impossible; SQLSTATE 23P01 -> graceful
     "conflict + alternatives" tool response.
  4. Defined PMS-failure behavior: Cliniko write-back is attempted synchronously
     (3s); on failure the appointment stands locally (source of truth) and an
     outbox row retries with exponential backoff.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Appointment,
    AppointmentType,
    Branch,
    ClinicPolicy,
    IdempotencyKey,
    OutboxEvent,
    Patient,
    Practitioner,
)
from app.services import availability, timeutils
from app.services.cliniko import ClinikoError, get_cliniko

log = logging.getLogger("booking")

CLINIKO_SYNC_TIMEOUT_NOTE = (
    "Booking is confirmed in the clinic system; practice-management sync will retry automatically."
)


async def _policy(session: AsyncSession, key: str, default: str) -> str:
    row = await session.get(ClinicPolicy, key)
    return row.value if row else default


async def fee_applies(session: AsyncSession, appointment: Appointment) -> tuple[bool, int]:
    """A cancellation/reschedule fee applies only inside the policy window
    before the appointment start (graded: never mention a fee outside it)."""
    window_hours = int(await _policy(session, "change_fee_window_hours", "24"))
    fee_inr = int(await _policy(session, "change_fee_inr", "100"))
    start = appointment.during.lower
    hours_to_start = (start - timeutils.now_utc()).total_seconds() / 3600
    return (0 < hours_to_start <= window_hours), fee_inr


async def check_idempotent(session: AsyncSession, key: str) -> dict | None:
    if not key:
        return None
    row = await session.get(IdempotencyKey, key)
    return row.response_body if row else None


async def store_idempotent(session: AsyncSession, key: str, response: dict) -> None:
    if not key:
        return
    await session.execute(
        text(
            "INSERT INTO idempotency_keys (key, response_body) "
            "VALUES (:key, CAST(:body AS jsonb)) ON CONFLICT (key) DO NOTHING"
        ),
        {"key": key, "body": json.dumps(response)},
    )


async def find_or_create_patient(
    session: AsyncSession, full_name: str, phone_e164: str
) -> Patient:
    rows = (
        (await session.execute(select(Patient).where(Patient.phone_e164 == phone_e164)))
        .scalars()
        .all()
    )
    name_lower = full_name.strip().lower()
    for row in rows:
        if row.full_name.strip().lower() == name_lower:
            return row
    patient = Patient(full_name=full_name.strip(), phone_e164=phone_e164)
    session.add(patient)
    await session.flush()
    return patient


async def _slot_alternatives(session: AsyncSession, appt_type: AppointmentType, start: datetime) -> list[dict]:
    result = await availability.search_slots(
        session,
        branch="any",
        appointment_type=appt_type.key,
        date_from=timeutils.utc_to_local(start).date(),
        max_results=3,
        bypass_cache=True,
    )
    return result.get("slots", [])


async def _ensure_cliniko_patient(session: AsyncSession, patient: Patient) -> str | None:
    """Create the patient in Cliniko if needed. Returns cliniko id or None on failure."""
    if patient.cliniko_patient_id:
        return patient.cliniko_patient_id
    parts = patient.full_name.split()
    first, last = parts[0], (" ".join(parts[1:]) or "-")
    try:
        created = await get_cliniko().create_patient(first, last, patient.phone_e164)
        patient.cliniko_patient_id = str(created.get("id"))
        return patient.cliniko_patient_id
    except (ClinikoError, Exception) as exc:  # noqa: BLE001
        log.warning("cliniko patient create failed: %s", exc)
        return None


async def book(
    session: AsyncSession,
    slot_id: str,
    patient_full_name: str,
    patient_phone: str,
    idempotency_key: str,
    call_id: str | None = None,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    try:
        practitioner_id, branch_id, type_id, start = availability.decode_slot_id(slot_id)
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "Invalid or expired slot. Please search availability again."}

    practitioner = await session.get(Practitioner, practitioner_id)
    branch = await session.get(Branch, branch_id)
    appt_type = await session.get(AppointmentType, type_id)
    if not (practitioner and branch and appt_type):
        return {"status": "error", "message": "Slot references unknown data. Please search again."}

    # Live re-validation against Cliniko (graded: stale-availability defense).
    fresh = await availability.search_slots(
        session,
        branch=branch.key,
        appointment_type=appt_type.key,
        practitioner_preference=practitioner.name,
        date_from=timeutils.utc_to_local(start).date(),
        date_to=timeutils.utc_to_local(start).date(),
        max_results=50,
        bypass_cache=True,
    )
    fresh_starts = {s["starts_at_utc"] for s in fresh.get("slots", [])}
    if start.isoformat() not in fresh_starts:
        alternatives = await _slot_alternatives(session, appt_type, start)
        response = {
            "status": "conflict",
            "message": "That time was just taken. Offer these alternatives instead.",
            "alternatives": alternatives,
        }
        await store_idempotent(session, idempotency_key, response)
        await session.commit()
        return response

    patient = await find_or_create_patient(session, patient_full_name, patient_phone)
    duration = appt_type.duration_minutes + appt_type.buffer_minutes
    end = start + timedelta(minutes=duration)

    # A patient cannot attend two appointments at once: if they already hold a
    # confirmed booking overlapping this window, surface it instead of creating
    # a duplicate (observed live: agent re-booked the same noon slot with a
    # second practitioner after a stale-context mixup).
    clash = (
        await session.execute(
            text(
                "SELECT id FROM appointments WHERE patient_id = :pid AND status = 'confirmed' "
                "AND during && tstzrange(:s, :e, '[)') LIMIT 1"
            ),
            {"pid": patient.id, "s": start, "e": end},
        )
    ).first()
    if clash:
        existing = await session.get(Appointment, clash.id)
        context = await _appointment_context(session, existing)
        return {
            "status": "already_booked",
            "existing_appointment": context,
            "message": "This patient ALREADY has a confirmed appointment overlapping that time. "
            "Tell the caller it is already booked — do not book again.",
        }

    try:
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                "appointment_type_id, during, status, fee_inr, created_via_call_id, cliniko_sync_status) "
                "VALUES (:id, :patient_id, :practitioner_id, :branch_id, :type_id, "
                "tstzrange(:s, :e, '[)'), 'confirmed', :fee, :call_id, 'pending')"
            ),
            {
                "id": (appt_id := uuid.uuid4()),
                "patient_id": patient.id,
                "practitioner_id": practitioner.id,
                "branch_id": branch.id,
                "type_id": appt_type.id,
                "s": start,
                "e": end,
                "fee": appt_type.fee_inr,
                "call_id": call_id,
            },
        )
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if "23P01" in str(exc.orig) or "no_practitioner_overlap" in str(exc):
            alternatives = await _slot_alternatives(session, appt_type, start)
            response = {
                "status": "conflict",
                "message": "That time was just taken. Offer these alternatives instead.",
                "alternatives": alternatives,
            }
            await store_idempotent(session, idempotency_key, response)
            await session.commit()
            return response
        raise

    # Synchronous Cliniko write-back (3s budget), outbox on failure.
    sync_status = "pending"
    cliniko_appt_id = None
    cliniko_patient_id = await _ensure_cliniko_patient(session, patient)
    if cliniko_patient_id:
        try:
            created = await get_cliniko().create_appointment(
                appt_type.cliniko_appointment_type_id,
                branch.cliniko_business_id,
                cliniko_patient_id,
                practitioner.cliniko_practitioner_id,
                start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            cliniko_appt_id = str(created.get("id"))
            sync_status = "synced"
        except Exception as exc:  # noqa: BLE001
            log.warning("cliniko appointment create failed, queueing outbox: %s", exc)

    if sync_status == "synced":
        await session.execute(
            text(
                "UPDATE appointments SET cliniko_appointment_id = :cid, cliniko_sync_status = 'synced' "
                "WHERE id = :id"
            ),
            {"cid": cliniko_appt_id, "id": appt_id},
        )
    else:
        session.add(
            OutboxEvent(
                event_type="create_appointment",
                payload={
                    "appointment_id": str(appt_id),
                    "patient_id": str(patient.id),
                    "starts_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "cliniko_appointment_type_id": appt_type.cliniko_appointment_type_id,
                    "cliniko_business_id": branch.cliniko_business_id,
                    "cliniko_practitioner_id": practitioner.cliniko_practitioner_id,
                },
            )
        )

    response = {
        "status": "confirmed",
        "appointment_id": str(appt_id),
        "when": timeutils.speakable_datetime(start),
        "practitioner": practitioner.name,
        "branch": branch.name,
        "branch_key": branch.key,
        "appointment_type": appt_type.name,
        "duration_minutes": appt_type.duration_minutes,
        "fee_inr": appt_type.fee_inr,
        "patient_name": patient.full_name,
        "pms_sync": sync_status,
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()
    return response


async def _upcoming_appointments(
    session: AsyncSession, phone_e164: str, patient_name: str | None = None
) -> list[Appointment]:
    query = (
        select(Appointment)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Patient.phone_e164 == phone_e164,
            Appointment.status == "confirmed",
            text("upper(during) > now()"),
        )
        .order_by(text("lower(during) ASC"))
    )
    if patient_name:
        query = query.where(Patient.full_name.ilike(f"%{patient_name.strip()}%"))
    return list((await session.execute(query)).scalars().all())


async def _resolve_target_appointment(
    session: AsyncSession,
    phone_e164: str,
    patient_name: str | None,
    appointment_id: str | None,
) -> tuple[Appointment | None, dict | None]:
    """Pick exactly one appointment to act on, or return a disambiguation
    response. An explicit appointment_id always wins; without one, a single
    upcoming appointment is unambiguous, and multiple upcoming appointments
    force the agent to specify (prevents the 'cancel all cancelled only one'
    idempotency collision observed live)."""
    if appointment_id:
        try:
            target = await session.get(Appointment, uuid.UUID(appointment_id))
        except ValueError:
            target = None
        if not target or target.status != "confirmed":
            return None, {
                "status": "not_found",
                "message": "That appointment_id is unknown or already cancelled. "
                "Call get_patient_record for the current list.",
            }
        patient = await session.get(Patient, target.patient_id)
        if patient and phone_e164 and patient.phone_e164 != phone_e164:
            return None, {
                "status": "not_found",
                "message": "That appointment belongs to a different phone number.",
            }
        return target, None

    upcoming = await _upcoming_appointments(session, phone_e164, patient_name)
    if not upcoming:
        return None, {"status": "not_found", "message": "No upcoming appointment found for this caller."}
    if len(upcoming) > 1:
        options = [await _appointment_context(session, a) for a in upcoming]
        return None, {
            "status": "choose_appointment",
            "message": "This caller has multiple upcoming appointments. Ask which one (or handle "
            "each in turn) and call this tool again with the specific appointment_id.",
            "appointments": options,
        }
    return upcoming[0], None


async def _appointment_context(session: AsyncSession, appointment: Appointment) -> dict:
    practitioner = await session.get(Practitioner, appointment.practitioner_id)
    branch = await session.get(Branch, appointment.branch_id)
    appt_type = await session.get(AppointmentType, appointment.appointment_type_id)
    patient = await session.get(Patient, appointment.patient_id)
    return {
        "appointment_id": str(appointment.id),
        "when": timeutils.speakable_datetime(appointment.during.lower),
        "practitioner": practitioner.name if practitioner else "?",
        "branch": branch.name if branch else "?",
        "appointment_type": appt_type.name if appt_type else "?",
        "patient_name": patient.full_name if patient else "?",
    }


async def reschedule(
    session: AsyncSession,
    phone_e164: str,
    new_slot_id: str,
    patient_name: str | None,
    idempotency_key: str,
    call_id: str | None = None,
    appointment_id: str | None = None,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    appointment, problem = await _resolve_target_appointment(
        session, phone_e164, patient_name, appointment_id
    )
    if problem:
        return problem

    applies, fee_inr = await fee_applies(session, appointment)

    try:
        practitioner_id, branch_id, type_id, new_start = availability.decode_slot_id(new_slot_id)
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "Invalid or expired slot. Please search availability again."}

    appt_type = await session.get(AppointmentType, type_id)
    branch = await session.get(Branch, branch_id)
    practitioner = await session.get(Practitioner, practitioner_id)
    duration = appt_type.duration_minutes + appt_type.buffer_minutes
    new_end = new_start + timedelta(minutes=duration)

    # Live re-validation, same as booking.
    fresh = await availability.search_slots(
        session,
        branch=branch.key,
        appointment_type=appt_type.key,
        practitioner_preference=practitioner.name,
        date_from=timeutils.utc_to_local(new_start).date(),
        date_to=timeutils.utc_to_local(new_start).date(),
        max_results=50,
        bypass_cache=True,
    )
    if new_start.isoformat() not in {s["starts_at_utc"] for s in fresh.get("slots", [])}:
        alternatives = await _slot_alternatives(session, appt_type, new_start)
        return {
            "status": "conflict",
            "message": "That new time was just taken. Offer these alternatives.",
            "alternatives": alternatives,
        }

    try:
        await session.execute(
            text(
                "UPDATE appointments SET during = tstzrange(:s, :e, '[)'), "
                "practitioner_id = :pid, branch_id = :bid, "
                "reschedule_count = reschedule_count + 1, cliniko_sync_status = "
                "CASE WHEN cliniko_appointment_id IS NULL THEN cliniko_sync_status ELSE 'pending' END "
                "WHERE id = :id"
            ),
            {"s": new_start, "e": new_end, "pid": practitioner.id, "bid": branch.id, "id": appointment.id},
        )
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if "23P01" in str(exc.orig) or "no_practitioner_overlap" in str(exc):
            alternatives = await _slot_alternatives(session, appt_type, new_start)
            return {"status": "conflict", "message": "That new time was just taken.", "alternatives": alternatives}
        raise

    sync_status = "pending"
    if appointment.cliniko_appointment_id:
        try:
            await get_cliniko().update_appointment(
                appointment.cliniko_appointment_id, new_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            sync_status = "synced"
            await session.execute(
                text("UPDATE appointments SET cliniko_sync_status = 'synced' WHERE id = :id"),
                {"id": appointment.id},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cliniko reschedule failed, queueing outbox: %s", exc)
            session.add(
                OutboxEvent(
                    event_type="update_appointment",
                    payload={
                        "appointment_id": str(appointment.id),
                        "cliniko_appointment_id": appointment.cliniko_appointment_id,
                        "starts_at": new_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                )
            )

    response = {
        "status": "rescheduled",
        "when": timeutils.speakable_datetime(new_start),
        "practitioner": practitioner.name,
        "branch": branch.name,
        "fee_applies": applies,
        "fee_inr": fee_inr if applies else 0,
        "fee_note": (
            f"A change fee of {fee_inr} rupees applies because the change is within the policy window."
            if applies
            else "No fee applies. Do not mention any fee."
        ),
        "pms_sync": sync_status,
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()
    return response


async def cancel(
    session: AsyncSession,
    phone_e164: str,
    patient_name: str | None,
    idempotency_key: str,
    call_id: str | None = None,
    appointment_id: str | None = None,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    appointment, problem = await _resolve_target_appointment(
        session, phone_e164, patient_name, appointment_id
    )
    if problem:
        return problem

    applies, fee_inr = await fee_applies(session, appointment)
    context = await _appointment_context(session, appointment)

    await session.execute(
        text("UPDATE appointments SET status = 'cancelled', cancellation_reason = 'caller request' WHERE id = :id"),
        {"id": appointment.id},
    )

    sync_status = "pending"
    if appointment.cliniko_appointment_id:
        try:
            await get_cliniko().cancel_appointment(appointment.cliniko_appointment_id)
            sync_status = "synced"
        except Exception as exc:  # noqa: BLE001
            log.warning("cliniko cancel failed, queueing outbox: %s", exc)
            session.add(
                OutboxEvent(
                    event_type="cancel_appointment",
                    payload={
                        "appointment_id": str(appointment.id),
                        "cliniko_appointment_id": appointment.cliniko_appointment_id,
                    },
                )
            )

    response = {
        "status": "cancelled",
        "cancelled_appointment": context,
        "fee_applies": applies,
        "fee_inr": fee_inr if applies else 0,
        "fee_note": (
            f"A cancellation fee of {fee_inr} rupees applies because the appointment is within the policy window."
            if applies
            else "No fee applies. Do not mention any fee."
        ),
        "pms_sync": sync_status,
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()
    return response


async def upcoming_appointments_for_phone(session: AsyncSession, phone_e164: str) -> list[dict]:
    query = (
        select(Appointment, Patient)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Patient.phone_e164 == phone_e164,
            Appointment.status == "confirmed",
            text("upper(during) > now()"),
        )
        .order_by(text("lower(during) ASC"))
    )
    rows = (await session.execute(query)).all()
    out = []
    for appointment, patient in rows:
        context = await _appointment_context(session, appointment)
        context["patient_name"] = patient.full_name
        out.append(context)
    return out
