"""Agent tool endpoints — the tool-calling schema between Retell and the backend.

Request body (Retell custom function): {"name": ..., "call": {...}, "args": {...}}.
Responses are JSON the LLM reads directly, so every response includes short
`message`/`note` strings telling the agent what to do next.

Idempotency: Retell retries failed/timed-out tool calls up to 2x with identical
(call_id, name, args) — the key is derived server-side from exactly that, so the
LLM never has to manage idempotency keys.
"""
import hashlib
import json
import logging
from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.db.models import Patient
from app.db.session import SessionLocal
from app.retell.security import verify_retell_request
from app.services import availability, booking
from app.services import sessions as sessions_svc
from app.services.phone import normalize_phone

log = logging.getLogger("tools")
router = APIRouter()


def _idempotency_key(call_id: str, name: str, args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{call_id}:{name}:{canonical}".encode()).hexdigest()


async def _parse(request: Request) -> tuple[str, str, dict]:
    """Returns (call_id, caller_phone_e164, args).

    Caller identity sources, in order: PSTN caller ID (from_number), the web
    page's simulated caller ID (call metadata), then whatever the agent passed
    explicitly. Without the metadata fallback, web calls would dead-end in
    need_phone even though the agent already knows the caller."""
    raw = await verify_retell_request(request)
    payload = json.loads(raw)
    call = payload.get("call", {}) or {}
    args = payload.get("args", {}) or {}
    call_id = call.get("call_id", args.get("_call_id", "direct"))
    metadata = call.get("metadata") or {}
    phone = normalize_phone(
        call.get("from_number") or metadata.get("simulated_phone") or args.get("patient_phone")
    )
    return call_id, phone, args


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


@router.post("/search_availability")
async def search_availability(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    async with SessionLocal() as session:
        result = await availability.search_slots(
            session,
            branch=(args.get("branch") or "any").lower(),
            appointment_type=args.get("appointment_type"),
            practitioner_preference=args.get("practitioner_preference"),
            date_from=_parse_date(args.get("date_from")),
            date_to=_parse_date(args.get("date_to")),
            weekday_mask=args.get("weekday_mask"),
            part_of_day=args.get("part_of_day"),
            time_earliest=args.get("time_earliest"),
            time_latest=args.get("time_latest"),
            earliest_available=bool(args.get("earliest_available")),
            max_results=int(args.get("max_results") or 3),
        )
        await sessions_svc.upsert_session(
            session,
            call_id,
            phone,
            stage="in_task",
            collected={
                "intent": "searching availability",
                **{k: str(v) for k, v in args.items() if v},
            },
        )
        await session.commit()
    if not result.get("slots"):
        result["message"] = (
            "No matching slots. Offer the nearest alternative day/branch or ask to widen preferences."
        )
    return result


@router.post("/book_appointment")
async def book_appointment(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    patient_phone = normalize_phone(args.get("patient_phone")) or phone
    name = (args.get("patient_full_name") or "").strip()
    if not name or len(name.split()) < 2:
        return {
            "status": "need_full_name",
            "message": "Ask for the caller's FULL name (first and last) before booking.",
        }
    if not patient_phone:
        return {
            "status": "need_phone",
            "message": "Ask for the caller's mobile number before booking.",
        }
    key = _idempotency_key(call_id, "book_appointment", args)
    async with SessionLocal() as session:
        result = await booking.book(
            session,
            slot_id=args.get("slot_id", ""),
            patient_full_name=name,
            patient_phone=patient_phone,
            idempotency_key=key,
            call_id=call_id,
        )
        if result.get("status") == "confirmed":
            await sessions_svc.upsert_session(
                session,
                call_id,
                phone or patient_phone,
                stage="completed",
                collected={"booked": result.get("when", ""), "patient": name},
            )
            await session.commit()
    return result


@router.post("/reschedule_appointment")
async def reschedule_appointment(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    patient_phone = normalize_phone(args.get("patient_phone")) or phone
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number first."}
    key = _idempotency_key(call_id, "reschedule_appointment", args)
    async with SessionLocal() as session:
        result = await booking.reschedule(
            session,
            phone_e164=patient_phone,
            new_slot_id=args.get("new_slot_id", ""),
            patient_name=args.get("patient_name"),
            idempotency_key=key,
            call_id=call_id,
            appointment_id=args.get("appointment_id"),
        )
        if result.get("status") == "rescheduled":
            await sessions_svc.upsert_session(
                session, call_id, phone or patient_phone, stage="completed",
                collected={"rescheduled_to": result.get("when", "")},
            )
            await session.commit()
    return result


@router.post("/cancel_appointment")
async def cancel_appointment(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    patient_phone = normalize_phone(args.get("patient_phone")) or phone
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number first."}
    key = _idempotency_key(call_id, "cancel_appointment", args)
    async with SessionLocal() as session:
        result = await booking.cancel(
            session,
            phone_e164=patient_phone,
            patient_name=args.get("patient_name"),
            idempotency_key=key,
            call_id=call_id,
            appointment_id=args.get("appointment_id"),
        )
        if result.get("status") == "cancelled":
            await sessions_svc.upsert_session(
                session, call_id, phone or patient_phone, stage="completed",
                collected={"cancelled": "true"},
            )
            await session.commit()
    return result


@router.post("/get_patient_record")
async def get_patient_record(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    patient_phone = normalize_phone(args.get("patient_phone")) or phone
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number."}
    async with SessionLocal() as session:
        patients = (
            (await session.execute(select(Patient).where(Patient.phone_e164 == patient_phone)))
            .scalars()
            .all()
        )
        upcoming = await booking.upcoming_appointments_for_phone(session, patient_phone)
    if not patients:
        return {
            "status": "new_patient",
            "message": "No record for this number. Treat as a new patient; collect full name.",
        }
    return {
        "status": "found",
        "patients_on_this_number": [p.full_name for p in patients],
        "multiple_patients": len(patients) > 1,
        "note": (
            "Multiple patients share this number — ask WHO the appointment is for before anything else."
            if len(patients) > 1
            else ""
        ),
        "upcoming_appointments": upcoming,
    }


@router.post("/log_followup_request")
async def log_followup_request(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    from sqlalchemy import text as sql_text

    async with SessionLocal() as session:
        await session.execute(
            sql_text(
                "INSERT INTO followup_tickets (id, phone_e164, patient_name, reason, urgency, call_id) "
                "VALUES (gen_random_uuid(), :phone, :name, :reason, :urgency, :call_id)"
            ),
            {
                "phone": normalize_phone(args.get("callback_number")) or phone or "unknown",
                "name": args.get("patient_name", ""),
                "reason": args.get("reason", "caller requested human follow-up"),
                "urgency": args.get("urgency", "normal"),
                "call_id": call_id,
            },
        )
        await session.commit()
    return {
        "status": "logged",
        "message": (
            "Follow-up logged. Tell the caller a staff member will call them back "
            "on this number — do NOT imply a live transfer is happening now."
        ),
    }
