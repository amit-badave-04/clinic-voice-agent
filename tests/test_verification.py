"""Verification service tests — dev-channel OTP flow against the live DB.

The dev channel (numbers under settings.otp_dev_prefix) generates and checks
codes locally, so these tests exercise the full challenge → check → session
lifecycle without sending SMS. The Twilio Verify channel differs only in who
validates the code.
"""
import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal
from app.services import verification

settings = get_settings()

TEST_PHONE = f"{settings.otp_dev_prefix}998"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with SessionLocal() as session:
        for table in ("verification_challenges", "verified_sessions", "auth_events"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE phone_e164 = :p"), {"p": TEST_PHONE}
            )
        await session.commit()


def _call_id() -> str:
    return f"test-verify-{uuid.uuid4().hex[:10]}"


async def test_dev_challenge_happy_path():
    call = _call_id()
    async with SessionLocal() as session:
        assert not await verification.is_verified(session, call, TEST_PHONE)
        sent = await verification.start_challenge(session, call, TEST_PHONE)
        await session.commit()
    assert sent["status"] == "code_sent"
    async with SessionLocal() as session:
        wrong = await verification.check_code(session, call, TEST_PHONE, "123456")
        await session.commit()
    assert wrong["status"] == "wrong_code"
    assert wrong["attempts_remaining"] == 2
    async with SessionLocal() as session:
        right = await verification.check_code(session, call, TEST_PHONE, settings.otp_dev_code)
        await session.commit()
    assert right["status"] == "verified"
    async with SessionLocal() as session:
        assert await verification.is_verified(session, call, TEST_PHONE)
        # A different call must NOT inherit the verification.
        assert not await verification.is_verified(session, "other-call", TEST_PHONE)


async def test_wrong_code_attempts_exhaust():
    call = _call_id()
    async with SessionLocal() as session:
        await verification.start_challenge(session, call, TEST_PHONE)
        await session.commit()
    for _ in range(3):
        async with SessionLocal() as session:
            result = await verification.check_code(session, call, TEST_PHONE, "999999")
            await session.commit()
    assert result["status"] == "wrong_code"
    assert result["attempts_remaining"] == 0
    async with SessionLocal() as session:
        blocked = await verification.check_code(session, call, TEST_PHONE, settings.otp_dev_code)
        await session.commit()
    assert blocked["status"] == "too_many_attempts"
    async with SessionLocal() as session:
        assert not await verification.is_verified(session, call, TEST_PHONE)


async def test_challenge_rate_limit_per_call():
    call = _call_id()
    for _ in range(verification.MAX_CHALLENGES_PER_CALL):
        async with SessionLocal() as session:
            sent = await verification.start_challenge(session, call, TEST_PHONE)
            await session.commit()
        assert sent["status"] == "code_sent"
    async with SessionLocal() as session:
        limited = await verification.start_challenge(session, call, TEST_PHONE)
        await session.commit()
    assert limited["status"] == "too_many_attempts"


async def test_check_without_challenge():
    async with SessionLocal() as session:
        result = await verification.check_code(session, _call_id(), TEST_PHONE, "000000")
        await session.commit()
    assert result["status"] == "no_active_code"
