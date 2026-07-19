"""Admission-control tests: kill switch (DB flag), daily web cap accounting,
Turnstile skip-when-unconfigured."""
import uuid

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import guard


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM clinic_policies WHERE key = 'kill_switch'"))
        await session.execute(text("DELETE FROM call_log WHERE call_id LIKE 'test-guard-%'"))
        await session.commit()


async def _set_flag(value: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO clinic_policies (key, value) VALUES ('kill_switch', :v) "
                "ON CONFLICT (key) DO UPDATE SET value = :v"
            ),
            {"v": value},
        )
        await session.commit()


async def test_kill_switch_db_flag():
    async with SessionLocal() as session:
        assert not await guard.kill_switch_on(session)
    await _set_flag("on")
    async with SessionLocal() as session:
        assert await guard.kill_switch_on(session)
        open_, reason = await guard.web_channel_open(session)
    assert not open_ and "paused" in reason
    await _set_flag("off")
    async with SessionLocal() as session:
        assert not await guard.kill_switch_on(session)


async def test_web_calls_today_counts_only_todays_web_calls():
    async with SessionLocal() as session:
        before = await guard.web_calls_today(session)
        await session.execute(
            text(
                "INSERT INTO call_log (call_id, direction, created_at) VALUES "
                "(:a, 'web', now()), (:b, 'inbound', now()), "
                "(:c, 'web', now() - interval '2 days')"
            ),
            {f: f"test-guard-{uuid.uuid4().hex[:8]}" for f in ("a", "b", "c")},
        )
        await session.commit()
    async with SessionLocal() as session:
        after = await guard.web_calls_today(session)
    assert after == before + 1  # only today's web call counts


async def test_turnstile_fails_closed_in_prod_open_in_dev(monkeypatch):
    # Unconfigured Turnstile must fail CLOSED in production (a bot gate that
    # silently disables itself is not a control) and open only in an explicit
    # non-production environment.
    monkeypatch.setattr(guard.settings, "turnstile_secret_key", "")
    monkeypatch.setattr(guard.settings, "environment", "production")
    assert await guard.verify_turnstile(None, "1.2.3.4") is False
    assert await guard.verify_turnstile("any-token", "1.2.3.4") is False
    monkeypatch.setattr(guard.settings, "environment", "development")
    assert await guard.verify_turnstile(None, "1.2.3.4") is True
    assert await guard.verify_turnstile("any-token", "1.2.3.4") is True
