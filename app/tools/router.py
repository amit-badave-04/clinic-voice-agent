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

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from app.db.models import Patient
from app.db.session import SessionLocal
from app.config import get_settings
from app.retell.security import verify_retell_request
from app.services import availability, booking, guard, timeutils
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


async def _parse_full(request: Request) -> tuple[str, str, dict, dict]:
    """Like _parse but also returns the conversation object (for endpoints
    that need call_type or metadata)."""
    call_id, phone, args, conv = await _parse_impl(request)
    return call_id, phone, args, conv


async def _parse(request: Request) -> tuple[str, str, dict]:
    call_id, phone, args, _ = await _parse_impl(request)
    return call_id, phone, args


async def _parse_impl(request: Request) -> tuple[str, str, dict, dict]:
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
    call_id = conv.get("call_id") or conv.get("chat_id") or payload.get("chat_id")
    if not call_id:
        # Conversation identity scopes BOTH the idempotency keys and the verified
        # session. It must come from the authenticated provider payload — never
        # from model-supplied args or a shared "direct" bucket, which would let
        # unrelated calls collide in idempotency/verification namespaces. Fail
        # closed rather than inventing an identity.
        raise HTTPException(status_code=400, detail="missing conversation id")
    metadata = conv.get("metadata") or {}
    phone = normalize_phone(
        conv.get("from_number") or metadata.get("simulated_phone") or args.get("patient_phone")
    )
    return call_id, phone, args, conv


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


MUTATING_TOOLS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}


def _rate_limited(call_id: str, phone: str, tool: str) -> dict | None:
    """Deterministic, model-independent abuse limits on the tool surface (H2).
    Returns a tool response for the agent when a budget is exceeded, else None.
    Buckets are process-local (single always-warm machine); global daily
    ceilings that must survive a restart live in the DB (see the booking day-cap
    and verification SMS-cap)."""
    # Two-hour window comfortably spans any single call, so a per-call_id bucket
    # effectively caps "per conversation".
    if not guard.rate_ok(f"call:{call_id}", settings.max_tool_calls_per_call, 7200):
        return _rate_limit_response()
    if tool == "search_availability" and not guard.rate_ok(
        f"call:{call_id}:search", settings.max_searches_per_call, 7200
    ):
        return _rate_limit_response()
    if tool in MUTATING_TOOLS:
        if not guard.rate_ok(f"call:{call_id}:mutate", settings.max_bookings_per_call, 7200):
            return _rate_limit_response()
        if phone and not guard.rate_ok(
            f"phone:{phone}:mutate", settings.max_mutations_per_phone_per_hour, 3600
        ):
            return _rate_limit_response()
    return None


def _rate_limit_response() -> dict:
    return {
        "status": "rate_limited",
        "message": (
            "There has been unusually high activity on this call, so I can't process that request "
            "right now. Apologize briefly and offer a staff callback (log_followup_request)."
        ),
    }


async def _daily_bookings_ok(session) -> bool:
    """Global ceiling on agent-created bookings per clinic-local day (H2)."""
    if not settings.max_bookings_per_day:
        return True
    from app.db.models import Appointment

    midnight_local = timeutils.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.source_system == "agent", Appointment.created_at >= midnight_local)
        )
    ).scalar_one()
    return count < settings.max_bookings_per_day


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
    limited = _rate_limited(call_id, phone, "search_availability")
    if limited:
        return limited
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
    # Identity precedence: the caller-ID / verified number wins; an
    # args-supplied number is only a fallback when caller ID is genuinely
    # unknown. Previously args won, letting the agent be steered to book onto an
    # arbitrary third party's line (H3).
    patient_phone = phone or normalize_phone(args.get("patient_phone"))
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
    limited = _rate_limited(call_id, patient_phone, "book_appointment")
    if limited:
        return limited
    # Name integrity gate (deterministic — the prompt's read-back rule is
    # advisory, this is the guarantee). Devanagari never reaches records;
    # implausible strings and near-matches of existing patients bounce back
    # for confirmation unless the agent passes name_confirmed after the
    # caller insisted.
    name = names_svc.normalize_for_records(name)
    key = _idempotency_key(call_id, "book_appointment", args)
    async with SessionLocal() as session:
        verified = await verification.is_verified(session, call_id, patient_phone)
        roster = (
            (await session.execute(select(Patient).where(Patient.phone_e164 == patient_phone)))
            .scalars()
            .all()
        )
        # Booking onto a number that already has records, when the caller ID is
        # unknown and unverified, requires proving possession of that number
        # first (stops record-poisoning of an arbitrary line via the web/edge
        # path where the number comes from args).
        if roster and not phone and not verified:
            denied = await _verification_gate(session, call_id, patient_phone)
            if denied:
                return denied
        if not settings.require_verification:
            verified = True  # gate disabled (dev): keep legacy disclosure behaviour
        if not await _daily_bookings_ok(session):
            return _rate_limit_response()
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
            suggestion = names_svc.roster_suggestion(name, [p.full_name for p in roster])
            if suggestion:
                # Only reveal the matching existing NAME to a caller verified for
                # this number (H3): otherwise a spoofed/guessed number would leak
                # who is on it. Unverified callers get a generic spelling prompt.
                if verified:
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
                return {
                    "status": "need_name_confirmation",
                    "heard_name": name,
                    "message": (
                        "Please double-check the spelling of the caller's full name with them, then "
                        "book again. If they confirm this exact spelling, call book_appointment again "
                        "with name_confirmed true."
                    ),
                }
        result = await booking.book(
            session,
            slot_id=args.get("slot_id", ""),
            patient_full_name=name,
            patient_phone=patient_phone,
            idempotency_key=key,
            call_id=call_id,
            disclose_existing=verified,
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
    patient_phone = phone or normalize_phone(args.get("patient_phone"))
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number first."}
    limited = _rate_limited(call_id, patient_phone, "reschedule_appointment")
    if limited:
        return limited
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
            patient_dob=args.get("patient_dob"),
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
    patient_phone = phone or normalize_phone(args.get("patient_phone"))
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number first."}
    limited = _rate_limited(call_id, patient_phone, "cancel_appointment")
    if limited:
        return limited
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
            patient_dob=args.get("patient_dob"),
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
    patient_phone = phone or normalize_phone(args.get("patient_phone"))
    if not patient_phone:
        return {"status": "need_phone", "message": "Ask for the caller's mobile number."}
    limited = _rate_limited(call_id, patient_phone, "get_patient_record")
    if limited:
        return limited
    async with SessionLocal() as session:
        denied = await _verification_gate(session, call_id, patient_phone)
        if denied:
            return denied
        # On a shared number, resolve to exactly ONE patient by name + DOB before
        # disclosing anything (M4). A verified caller no longer receives the full
        # roster or a co-tenant's appointments.
        patient, problem = await booking.resolve_patient_on_number(
            session, patient_phone, args.get("patient_name"), args.get("patient_dob")
        )
        if problem:
            return problem
        if patient is None:
            return {
                "status": "new_patient",
                "message": "No record for this number. Treat as a new patient; collect full name.",
            }
        upcoming = await booking.upcoming_appointment_dicts_for_patient(session, patient.id)
    return {
        "status": "found",
        "patient_name": patient.full_name,
        "upcoming_appointments": upcoming,
    }


@router.post("/resolve_live_transfer")
async def resolve_live_transfer(request: Request) -> dict:
    call_id, phone, args, conv = await _parse_full(request)
    limited = _rate_limited(call_id, phone, "resolve_live_transfer")
    if limited:
        return limited
    from app.services import transfer

    is_phone_call = conv.get("call_type") == "phone_call"
    async with SessionLocal() as session:
        plan = await transfer.build_plan(
            session, call_id, phone, is_phone_call, str(args.get("reason") or "caller asked for a human")
        )
        await session.commit()
    return plan


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
    limited = _rate_limited(call_id, phone, "send_verification_code")
    if limited:
        return limited
    async with SessionLocal() as session:
        result = await verification.start_challenge(session, call_id, phone)
        await session.commit()
    return result


@router.post("/check_verification_code")
async def check_verification_code(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    if not phone:
        return {"status": "need_phone", "message": "No caller number on this call."}
    limited = _rate_limited(call_id, phone, "check_verification_code")
    if limited:
        return limited
    async with SessionLocal() as session:
        result = await verification.check_code(session, call_id, phone, str(args.get("code") or ""))
        await session.commit()
    return result


@router.post("/log_followup_request")
async def log_followup_request(request: Request) -> dict:
    call_id, phone, args = await _parse(request)
    limited = _rate_limited(call_id, phone, "log_followup_request")
    if limited:
        return limited
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
    from app.services import alerts

    callback_phone = normalize_phone(args.get("callback_number")) or phone or "unknown"
    # Eval fixtures and demo personas live on the fictional dev prefix — their
    # tickets are durable like any other, but they must not page the operator.
    if not callback_phone.startswith(settings.otp_dev_prefix):
        alerts.notify_bg(
            f"📞 Callback owed: {args.get('patient_name') or 'caller'} at "
            f"{callback_phone} — {args.get('reason', 'requested human follow-up')[:200]}"
        )
    return {
        "status": "logged",
        "message": (
            "Follow-up logged. Tell the caller a staff member will call them back "
            "on this number — do NOT imply a live transfer is happening now."
        ),
    }
