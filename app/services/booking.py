"""Booking / reschedule / cancel — the write path.

Guarantees (in order):
  1. Idempotency: Retell retries tool calls; an idempotency_keys row (stored in
     the SAME transaction as the write) makes replays return the original result.
  2. Live re-validation: booking never trusts slot data sitting in the LLM's
     context — the slot is re-checked against Cliniko + local DB at write time.
  3. Write-time conflict enforcement: the GiST exclusion constraint makes a
     double-booking structurally impossible; SQLSTATE 23P01 -> graceful
     "conflict + alternatives" tool response.
  4. No external I/O inside a database transaction: every write commits locally
     with an outbox event queued atomically, then the event is drained inline
     (fast path) — the background worker retries anything that fails. This is
     the defined behavior when the PMS write-back fails: the local booking
     stands (source of truth) and sync retries with exponential backoff.
"""
import binascii
import json
import logging
import uuid
from datetime import date, datetime, timedelta

from rapidfuzz.distance import JaroWinkler
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Appointment,
    AppointmentType,
    Branch,
    ClinicPolicy,
    IdempotencyKey,
    Patient,
    Practitioner,
)
from app.services import availability, timeutils
from app.services import outbox as outbox_svc

log = logging.getLogger("booking")

# Everything decode_slot_id can legitimately raise on malformed/forged input.
SLOT_DECODE_ERRORS = (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error)


async def _policy(session, key: str, default: str) -> str:
    row = await session.get(ClinicPolicy, key)
    return row.value if row else default


async def fee_applies(session, appointment: Appointment) -> tuple[bool, int]:
    """A cancellation/reschedule fee applies only inside the policy window
    before the appointment start (policy: the agent must never mention a fee outside it)."""
    window_hours = int(await _policy(session, "change_fee_window_hours", "24"))
    fee_inr = int(await _policy(session, "change_fee_inr", "100"))
    start = appointment.during.lower
    hours_to_start = (start - timeutils.now_utc()).total_seconds() / 3600
    return (0 < hours_to_start <= window_hours), fee_inr


async def check_idempotent(session, key: str) -> dict | None:
    if not key:
        return None
    row = await session.get(IdempotencyKey, key)
    return row.response_body if row else None


async def store_idempotent(session, key: str, response: dict) -> None:
    if not key:
        return
    await session.execute(
        text(
            "INSERT INTO idempotency_keys (key, response_body) "
            "VALUES (:key, CAST(:body AS jsonb)) ON CONFLICT (key) DO NOTHING"
        ),
        {"key": key, "body": json.dumps(response)},
    )


async def _enqueue(session, event_type: str, appointment_id: uuid.UUID) -> int:
    """Queue a Cliniko write-back event in the CURRENT transaction (atomic with
    the local write). Returns the outbox id for the inline fast-path drain."""
    row = (
        await session.execute(
            text(
                "INSERT INTO outbox (event_type, payload) "
                "VALUES (:event_type, CAST(:payload AS jsonb)) RETURNING id"
            ),
            {"event_type": event_type, "payload": json.dumps({"appointment_id": str(appointment_id)})},
        )
    ).first()
    return int(row.id)


def parse_dob(value: str | None) -> date | None:
    """Parse a caller-supplied date of birth (YYYY-MM-DD or a leading ISO date)."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError):
        return None


def _patient_factor_ok(patient: Patient, patient_name: str | None, dob: date | None) -> bool:
    """The per-patient factor for a shared "family line" number (M4). OTP already
    proved possession of the NUMBER; this proves WHICH co-tenant is calling.
    Fails closed: a patient with no DOB on file cannot be selected this way."""
    if patient.date_of_birth is None or dob is None:
        return False
    if patient.date_of_birth != dob:
        return False
    if not patient_name:
        return False
    target = patient_name.casefold().strip()
    name = patient.full_name.casefold().strip()
    return target in name or name in target or JaroWinkler.normalized_similarity(target, name) >= 0.8


async def resolve_patient_on_number(
    session,
    phone_e164: str,
    patient_name: str | None,
    patient_dob: str | None,
) -> tuple[Patient | None, dict | None]:
    """Which patient on this number the caller is authorized to act as/for.

    - no patients: (None, None) — the caller (get_patient_record / booking)
      handles the new-patient case.
    - exactly one patient: that patient, no extra factor (OTP possession of the
      sole record is unambiguous).
    - multiple patients (family line): require a matching full name AND date of
      birth to select exactly one; otherwise a disambiguation response that
      discloses NO names. This stops a verified holder of a shared number from
      reading or changing a different co-tenant's appointments."""
    patients = (
        (await session.execute(select(Patient).where(Patient.phone_e164 == phone_e164)))
        .scalars()
        .all()
    )
    if not patients:
        return None, None
    if len(patients) == 1:
        return patients[0], None
    dob = parse_dob(patient_dob)
    matches = [p for p in patients if _patient_factor_ok(p, patient_name, dob)]
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "status": "need_patient_identification",
        "multiple_patients": True,
        "message": (
            "More than one patient uses this number. For privacy, do NOT reveal any names. "
            "Ask the caller for the specific patient's FULL name AND date of birth, then call "
            "this tool again with patient_name and patient_dob."
        ),
    }


async def _upcoming_appointments_for_patient(session, patient_id) -> list[Appointment]:
    query = (
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status == "confirmed",
            text("upper(during) > now()"),
        )
        .order_by(text("lower(during) ASC"))
    )
    return list((await session.execute(query)).scalars().all())


async def find_or_create_patient(session, full_name: str, phone_e164: str) -> Patient:
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


async def _slot_alternatives(session, appt_type: AppointmentType, start: datetime) -> list[dict]:
    result = await availability.search_slots(
        session,
        branch="any",
        appointment_type=appt_type.key,
        date_from=timeutils.utc_to_local(start).date(),
        max_results=3,
        bypass_cache=True,
    )
    return result.get("slots", [])


async def book(
    session,
    slot_id: str,
    patient_full_name: str,
    patient_phone: str,
    idempotency_key: str,
    call_id: str | None = None,
    disclose_existing: bool = True,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    try:
        practitioner_id, branch_id, type_id, start = availability.decode_slot_id(slot_id)
    except SLOT_DECODE_ERRORS:
        return {"status": "error", "message": "Invalid or expired slot. Please search availability again."}

    practitioner = await session.get(Practitioner, practitioner_id)
    branch = await session.get(Branch, branch_id)
    appt_type = await session.get(AppointmentType, type_id)
    if not (practitioner and branch and appt_type):
        return {"status": "error", "message": "Slot references unknown data. Please search again."}

    # Live re-validation against Cliniko (stale-availability defense).
    # Read-only — no locks are held at this point.
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
    # a duplicate.
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
        # Do not disclose the existing appointment's details to an unverified
        # caller (H3): confirming "there is already a booking for <name> at
        # <time>" would leak another patient's record on a spoofed/guessed
        # number. Verified self-service still gets the full context.
        if not disclose_existing:
            return {
                "status": "already_booked",
                "message": "There is already a booking overlapping that time on this number. "
                "Offer the caller a different time.",
            }
        existing = await session.get(Appointment, clash.id)
        context = await _appointment_context(session, existing)
        return {
            "status": "already_booked",
            "existing_appointment": context,
            "message": "This patient ALREADY has a confirmed appointment overlapping that time. "
            "Tell the caller it is already booked — do not book again.",
        }

    appt_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                "appointment_type_id, during, status, fee_inr, created_via_call_id, cliniko_sync_status) "
                "VALUES (:id, :patient_id, :practitioner_id, :branch_id, :type_id, "
                "tstzrange(:s, :e, '[)'), 'confirmed', :fee, :call_id, 'pending')"
            ),
            {
                "id": appt_id,
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

    outbox_id = await _enqueue(session, "create_appointment", appt_id)
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
        "pms_sync": "pending",
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()  # local truth is durable BEFORE any external I/O

    synced = await outbox_svc.process_event_inline(outbox_id)
    response["pms_sync"] = "synced" if synced else "pending"
    if not synced:
        # Disclosed hold: the local booking stands and sync will retry, but the
        # caller must not hear an unqualified "confirmed" for a write the
        # clinic's calendar hasn't acknowledged yet.
        response["sync_note"] = (
            "The clinic's calendar system is responding slowly. Present this as RESERVED: "
            "the change is saved and the clinic will confirm it shortly by phone or SMS. "
            "Do not call it fully confirmed, and do not retry the tool."
        )
    return response


async def _upcoming_appointments(
    session, phone_e164: str, patient_name: str | None = None
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
    session,
    phone_e164: str,
    patient_name: str | None,
    appointment_id: str | None,
    patient_dob: str | None = None,
) -> tuple[Appointment | None, dict | None]:
    """Pick exactly one appointment to act on, or return a disambiguation
    response. On a shared "family line" number the caller must first be resolved
    to a single co-tenant (name + DOB); an appointment_id may target only that
    resolved patient — a verified holder of the number cannot act on another
    person's appointment (M4)."""
    actor, problem = await resolve_patient_on_number(session, phone_e164, patient_name, patient_dob)
    if problem:
        return None, problem

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
        # On a shared number, the appointment must belong to the identified patient.
        if actor is not None and target.patient_id != actor.id:
            return None, {
                "status": "not_found",
                "message": "That appointment belongs to a different patient on this number.",
            }
        return target, None

    if actor is not None:
        upcoming = await _upcoming_appointments_for_patient(session, actor.id)
    else:
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


async def _appointment_context(session, appointment: Appointment) -> dict:
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
    session,
    phone_e164: str,
    new_slot_id: str,
    patient_name: str | None,
    idempotency_key: str,
    call_id: str | None = None,
    appointment_id: str | None = None,
    patient_dob: str | None = None,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    appointment, problem = await _resolve_target_appointment(
        session, phone_e164, patient_name, appointment_id, patient_dob
    )
    if problem:
        return problem

    applies, fee_inr = await fee_applies(session, appointment)  # window judged on the ORIGINAL time

    try:
        practitioner_id, branch_id, type_id, new_start = availability.decode_slot_id(new_slot_id)
    except SLOT_DECODE_ERRORS:
        return {"status": "error", "message": "Invalid or expired slot. Please search availability again."}

    appt_type = await session.get(AppointmentType, type_id)
    branch = await session.get(Branch, branch_id)
    practitioner = await session.get(Practitioner, practitioner_id)
    if not (practitioner and branch and appt_type):
        return {"status": "error", "message": "Slot references unknown data. Please search again."}
    duration = appt_type.duration_minutes + appt_type.buffer_minutes
    new_end = new_start + timedelta(minutes=duration)

    # Live re-validation, same as booking. Read-only, no locks held.
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
                "reschedule_count = reschedule_count + 1, cliniko_sync_status = 'pending' "
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

    outbox_id = await _enqueue(session, "update_appointment", appointment.id)
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
        "pms_sync": "pending",
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()

    synced = await outbox_svc.process_event_inline(outbox_id)
    response["pms_sync"] = "synced" if synced else "pending"
    if not synced:
        # Disclosed hold: the local booking stands and sync will retry, but the
        # caller must not hear an unqualified "confirmed" for a write the
        # clinic's calendar hasn't acknowledged yet.
        response["sync_note"] = (
            "The clinic's calendar system is responding slowly. Present this as RESERVED: "
            "the change is saved and the clinic will confirm it shortly by phone or SMS. "
            "Do not call it fully confirmed, and do not retry the tool."
        )
    return response


async def cancel(
    session,
    phone_e164: str,
    patient_name: str | None,
    idempotency_key: str,
    call_id: str | None = None,
    appointment_id: str | None = None,
    patient_dob: str | None = None,
) -> dict:
    cached = await check_idempotent(session, idempotency_key)
    if cached:
        return cached

    appointment, problem = await _resolve_target_appointment(
        session, phone_e164, patient_name, appointment_id, patient_dob
    )
    if problem:
        return problem

    applies, fee_inr = await fee_applies(session, appointment)
    context = await _appointment_context(session, appointment)

    await session.execute(
        text(
            "UPDATE appointments SET status = 'cancelled', cancellation_reason = 'caller request', "
            "cliniko_sync_status = 'pending' WHERE id = :id"
        ),
        {"id": appointment.id},
    )
    outbox_id = await _enqueue(session, "cancel_appointment", appointment.id)
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
        "pms_sync": "pending",
    }
    await store_idempotent(session, idempotency_key, response)
    await session.commit()

    synced = await outbox_svc.process_event_inline(outbox_id)
    response["pms_sync"] = "synced" if synced else "pending"
    if not synced:
        # Disclosed hold: the local booking stands and sync will retry, but the
        # caller must not hear an unqualified "confirmed" for a write the
        # clinic's calendar hasn't acknowledged yet.
        response["sync_note"] = (
            "The clinic's calendar system is responding slowly. Present this as RESERVED: "
            "the change is saved and the clinic will confirm it shortly by phone or SMS. "
            "Do not call it fully confirmed, and do not retry the tool."
        )
    return response


async def upcoming_appointment_dicts_for_patient(session, patient_id) -> list[dict]:
    """Speakable contexts for one patient's upcoming appointments (used by the
    verification-gated get_patient_record — scoped to a single identified
    patient so a shared number never dumps a co-tenant's schedule)."""
    appts = await _upcoming_appointments_for_patient(session, patient_id)
    return [await _appointment_context(session, a) for a in appts]


async def upcoming_appointments_for_phone(session, phone_e164: str) -> list[dict]:
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
