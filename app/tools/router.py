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
from datetime import date, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.db.models import Patient
from app.db.session import SessionLocal
from app.config import get_settings
from app.retell.security import verify_retell_request
from app.services import availability, booking
from app.services import names as names_svc
from app.services import sessions as sessions_svc
from app.services import verification
from app.services.phone import normalize_phone

settings = get_settings()

log = logging.getLogger("tools")
router = APIRouter()


# Args the platform generates per-invocation (not semantic to the action).
# They must not influence idempotency: a platform retry that regenerates the
# filler text must still dedupe against the original attempt.
NON_SEMANTIC_ARGS = {"execution_message"}


def _idempotency_key(call_id: str, name: str, args: dict) -> str:
    semantic = {k: v for k, v in args.items() if k not in NON_SEMANTIC_ARGS}
    canonical = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{call_id}:{name}:{canonical}".encode()).hexdigest()


async def _parse(request: Request) -> tuple[str, str, dict]:
    """Returns (call_id, caller_phone_e164, args).

    Caller identity sources, in order: PSTN caller ID (from_number), the web
    page's simulated caller ID (call metadata), then whatever the agent passed
    explicitly. Without the metadata fallback, web calls would dead-end in
    need_phone even though the agent already knows the caller."""
    raw = await verify_retell_request(request)
    payload = json.loads(raw)
    args = payload.get("args", {}) or {}
    # Conversation identity scopes the idempotency keys. Voice calls send the
    # conversation object as payload["call"] with call_id; the chat channel
    # may send it as payload["chat"] with chat_id. Without a per-conversation
    # id, keys collide ACROSS conversations whenever args coincide (found by
    # the eval harness: a booking was served from another conversation's
    # cached response and nothing was inserted).
    conv = payload.get("call") or payload.get("chat") or {}
    call_id = (
        conv.get("call_id") or conv.get("chat_id") or payload.get("chat_id")
        or args.get("_call_id") or "direct"
    )
    metadata = conv.get("metadata") or {}
    phone = normalize_phone(
        conv.get("from_number") or metadata.get("simulated_phone") or args.get("patient_phone")
    )
    return call_id, phone, args


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


async def _verification_gate(session, call_id: str, phone: str) -> dict | None:
    """Caller ID is a routing hint, not identity proof. Existing-appointment
    disclosure/changes require an in-call OTP; returns the tool response that
    walks the agent through it, or None when the call is already verified."""
    if not settings.require_verification:
        return None
    if await verification.is_verified(session, call_id, phone):
        return None
    from app.db.models import AuthEvent

    session.add(AuthEvent(call_id=call_id, phone_e164=phone, event="denied_unverified"))
    await session.commit()
    return {
        "status": "verification_required",
        "message": (
            "Identity is not verified on this call. Do NOT confirm whether any appointment "
            "exists. Say that for privacy you will first send a six-digit code by SMS to the "
            "number on file, call send_verification_code, have them enter it on the keypad, "
            "check it with check_verification_code, then retry this request."
        ),
    }


def _only_sundays(date_from: date | None, date_to: date | None) -> bool:
    """True when the searched window contains nothing but Sundays (the clinic's
    closed day) — the agent should say WHY there are no slots, not just shrug."""
    if not date_from:
        return False
    date_to = date_to or date_from
    if date_to < date_from or (date_to - date_from).days > 14:
        return False
    day = date_from
    while day <= date_to:
        if day.weekday() != 6:
            return False
        day += timedelta(days=1)
    return True


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
        if _only_sundays(_parse_date(args.get("date_from")), _parse_date(args.get("date_to"))):
            result["message"] = (
                "That date is a Sunday and the clinic is CLOSED on Sundays. Tell the caller that "
                "(warmly) and offer Monday or another weekday instead."
            )
        else:
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
    # Name integrity gate (deterministic — the prompt's read-back rule is
    # advisory, this is the guarantee). Devanagari never reaches records;
    # implausible strings and near-matches of existing patients bounce back
    # for confirmation unless the agent passes name_confirmed after the
    # caller insisted.
    name = names_svc.normalize_for_records(name)
    key = _idempotency_key(call_id, "book_appointment", args)
    async with SessionLocal() as session:
        if not args.get("name_confirmed"):
            if not names_svc.is_plausible(name):
                return {
                    "status": "implausible_name",
                    "heard_name": name,
                    "message": (
                        "This does not look like a person's name — it was probably misheard. "
                        "Apologize, ask the caller to repeat or spell their name, then book with "
                        "the corrected name. Only if the caller insists this IS their real name, "
                        "call book_appointment again with name_confirmed true."
                    ),
                }
            roster = (
                (await session.execute(select(Patient).where(Patient.phone_e164 == patient_phone)))
                .scalars()
                .all()
            )
            suggestion = names_svc.roster_suggestion(name, [p.full_name for p in roster])
            if suggestion:
                return {
                    "status": "need_name_confirmation",
                    "heard_name": name,
                    "suggested_match": suggestion,
                    "message": (
                        f"A patient named '{suggestion}' already exists on this number — the caller "
                        "is probably the same person misheard. Ask which is correct. If they confirm "
                        f"'{suggestion}', book with that exact name; if they insist on the new name, "
                        "call book_appointment again with name_confirmed true."
                    ),
                }
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
        denied = await _verification_gate(session, call_id, patient_phone)
        if denied:
            return denied
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
        denied = await _verification_gate(session, call_id, patient_phone)
        if denied:
            return denied
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
        denied = await _verification_gate(session, call_id, patient_phone)
        if denied:
            return denied
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


@router.post("/send_verification_code")
async def send_verification_code(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    # The OTP goes ONLY to the caller-ID / number on file — never to a number
    # the conversation supplied, which would hand the factor to the attacker.
    if not phone:
        return {
            "status": "need_phone",
            "message": "No caller number on this call — verification by SMS is not possible. Offer a staff callback instead.",
        }
    async with SessionLocal() as session:
        result = await verification.start_challenge(session, call_id, phone)
        await session.commit()
    return result


@router.post("/check_verification_code")
async def check_verification_code(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    if not phone:
        return {"status": "need_phone", "message": "No caller number on this call."}
    async with SessionLocal() as session:
        result = await verification.check_code(session, call_id, phone, str(args.get("code") or ""))
        await session.commit()
    return result


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
