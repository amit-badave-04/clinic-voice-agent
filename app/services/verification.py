"""Caller identity verification: OTP to the number on file, checked in-call.

Caller ID is treated as a routing hint, not proof of identity. Before existing
appointments are disclosed or changed, the caller must confirm a six-digit
code sent to the number already on record (never to a caller-supplied number —
that would hand the factor to the attacker). A confirmed code mints a
short-lived VerifiedSession scoped to this call; the tool router enforces it.

Two delivery channels:
  - 'sms'  — Twilio Verify generates, delivers and checks the code (no code
             material stored here beyond the challenge row).
  - 'dev'  — numbers under settings.otp_dev_prefix (demo personas and eval
             fixtures: fictional patients on unreachable numbers) use a fixed
             code, stored hashed, checked locally.

Every event lands in auth_events (append-only audit ledger).
"""
import hashlib
import logging
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AuthEvent, VerificationChallenge, VerifiedSession
from app.services import timeutils

log = logging.getLogger("verification")
settings = get_settings()

TWILIO_VERIFY = "https://verify.twilio.com/v2"
MAX_CHALLENGES_PER_CALL = 3


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _is_dev_number(phone: str) -> bool:
    return phone.startswith(settings.otp_dev_prefix)


async def _log(session: AsyncSession, call_id: str, phone: str, event: str, detail: str = "") -> None:
    session.add(AuthEvent(call_id=call_id, phone_e164=phone, event=event, detail=detail))


async def is_verified(session: AsyncSession, call_id: str, phone: str) -> bool:
    row = (
        await session.execute(
            select(VerifiedSession).where(
                VerifiedSession.call_id == call_id,
                VerifiedSession.phone_e164 == phone,
                VerifiedSession.expires_at > timeutils.now_utc(),
            )
        )
    ).scalars().first()
    return row is not None


async def start_challenge(session: AsyncSession, call_id: str, phone: str) -> dict:
    """Send (or arm, for dev numbers) an OTP for this call. Returns a tool
    response the agent can act on directly."""
    sent_count = len(
        (
            await session.execute(
                select(VerificationChallenge.id).where(VerificationChallenge.call_id == call_id)
            )
        ).all()
    )
    if sent_count >= MAX_CHALLENGES_PER_CALL:
        await _log(session, call_id, phone, "challenge_rate_limited")
        return {
            "status": "too_many_attempts",
            "message": (
                "Verification attempts exhausted for this call. Offer a staff callback "
                "(log_followup_request) instead — do not try again."
            ),
        }

    challenge = VerificationChallenge(
        id=uuid.uuid4(),
        call_id=call_id,
        phone_e164=phone,
        expires_at=timeutils.now_utc() + timedelta(minutes=10),
    )
    if _is_dev_number(phone):
        challenge.channel = "dev"
        challenge.code_hash = _hash(settings.otp_dev_code)
    else:
        # Global daily SMS ceiling: caps Twilio spend and bounds SMS-bombing of
        # third parties even across rotated call-ids/IPs (dev numbers are exempt
        # — they never send real SMS). Enforced in the DB so it survives a
        # restart, unlike the in-process per-caller limits.
        if settings.max_sms_per_day:
            midnight_local = timeutils.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
            sms_today = (
                await session.execute(
                    select(func.count())
                    .select_from(VerificationChallenge)
                    .where(
                        VerificationChallenge.channel == "sms",
                        VerificationChallenge.created_at >= midnight_local,
                    )
                )
            ).scalar_one()
            if sms_today >= settings.max_sms_per_day:
                log.warning("daily SMS ceiling reached (%s) — refusing OTP to %s", sms_today, phone)
                await _log(session, call_id, phone, "challenge_send_failed", "daily sms ceiling")
                return {
                    "status": "sms_unavailable",
                    "message": (
                        "Code delivery is unavailable right now. Apologize and offer a staff "
                        "callback (log_followup_request) to handle their request."
                    ),
                }
        if not (settings.twilio_verify_service_sid and settings.twilio_account_sid):
            await _log(session, call_id, phone, "challenge_send_failed", "verify service not configured")
            return {
                "status": "sms_unavailable",
                "message": (
                    "Code delivery is unavailable right now. Apologize and offer a staff "
                    "callback (log_followup_request) to handle their request."
                ),
            }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    f"{TWILIO_VERIFY}/Services/{settings.twilio_verify_service_sid}/Verifications",
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                    data={"To": phone, "Channel": "sms"},
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("verify send failed for %s: %s", phone, exc)
            await _log(session, call_id, phone, "challenge_send_failed", str(exc)[:200])
            return {
                "status": "sms_unavailable",
                "message": (
                    "The SMS could not be sent. Apologize and offer a staff callback "
                    "(log_followup_request) to handle their request."
                ),
            }
    session.add(challenge)
    await _log(session, call_id, phone, "challenge_sent", challenge.channel)
    return {
        "status": "code_sent",
        "message": (
            "A six-digit code was sent by SMS to the caller's number on file. Ask them to "
            "enter it on their phone keypad (or read it out), then call "
            "check_verification_code with the digits."
        ),
    }


async def check_code(session: AsyncSession, call_id: str, phone: str, code: str) -> dict:
    """Validate a code for the newest open challenge on this call."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    challenge = (
        await session.execute(
            select(VerificationChallenge)
            .where(
                VerificationChallenge.call_id == call_id,
                VerificationChallenge.phone_e164 == phone,
                VerificationChallenge.consumed_at.is_(None),
            )
            .order_by(VerificationChallenge.created_at.desc())
        )
    ).scalars().first()
    if challenge is None or challenge.expires_at < timeutils.now_utc():
        await _log(session, call_id, phone, "code_bad", "no open challenge")
        return {
            "status": "no_active_code",
            "message": "No active code for this call. Send a fresh one with send_verification_code.",
        }
    if challenge.attempt_count >= challenge.max_attempts:
        await _log(session, call_id, phone, "code_bad", "attempts exhausted")
        return {
            "status": "too_many_attempts",
            "message": (
                "Too many wrong attempts. Offer a staff callback (log_followup_request) — "
                "do not verify on this call."
            ),
        }
    challenge.attempt_count += 1

    if challenge.channel == "dev":
        ok = bool(code) and _hash(code) == challenge.code_hash
    else:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    f"{TWILIO_VERIFY}/Services/{settings.twilio_verify_service_sid}/VerificationCheck",
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                    data={"To": phone, "Code": code},
                )
            ok = response.status_code == 200 and response.json().get("status") == "approved"
        except httpx.HTTPError as exc:
            log.warning("verify check failed for %s: %s", phone, exc)
            ok = False

    if not ok:
        remaining = challenge.max_attempts - challenge.attempt_count
        await _log(session, call_id, phone, "code_bad", f"remaining={remaining}")
        return {
            "status": "wrong_code",
            "attempts_remaining": remaining,
            "message": (
                "That code is not correct. Ask them to re-check the SMS and try again."
                if remaining > 0
                else "That was the last attempt. Offer a staff callback (log_followup_request)."
            ),
        }

    challenge.consumed_at = timeutils.now_utc()
    session.add(
        VerifiedSession(
            id=uuid.uuid4(),
            call_id=call_id,
            phone_e164=phone,
            method="sms_otp" if challenge.channel == "sms" else "dev_otp",
            expires_at=timeutils.now_utc() + timedelta(minutes=settings.verified_session_ttl_minutes),
        )
    )
    await _log(session, call_id, phone, "code_ok", challenge.channel)
    return {
        "status": "verified",
        "message": (
            "Caller verified for this call. Continue with their existing-appointment "
            "request — do not ask them to verify again on this call."
        ),
    }
