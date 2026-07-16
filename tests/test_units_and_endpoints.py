"""Unit + endpoint tests from the code-review gap list:
  - normalize_phone including the garbage-in case
  - slot filter boundaries (12:00 is afternoon, 17:00 is evening)
  - idempotency key ignores platform-generated execution_message
  - upsert_session survives concurrent first-writes (chat channel race)
  - tool endpoints reject unauthenticated requests (HMAC / shared secret)
  - call_ended webhook is idempotent across duplicate deliveries
  - availability subtracts locally-confirmed bookings from Cliniko slots
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal
from app.services import availability, timeutils
from app.services.phone import normalize_phone
from app.tools.router import _idempotency_key

settings = get_settings()

RACE_CALL_ID = "test-upsert-race"
WEBHOOK_CALL_ID = "test-ended-idempotent"
WEBHOOK_PHONE = "+919000000981"
SUBTRACT_PHONE = "+919000000982"


@pytest.fixture(autouse=True)
async def cleanup():
    yield
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM call_sessions WHERE call_id IN (:a, :b)"),
            {"a": RACE_CALL_ID, "b": WEBHOOK_CALL_ID},
        )
        await session.execute(text("DELETE FROM call_log WHERE call_id = :c"), {"c": WEBHOOK_CALL_ID})
        await session.execute(
            text(
                "DELETE FROM appointments WHERE patient_id IN "
                "(SELECT id FROM patients WHERE phone_e164 = :p)"
            ),
            {"p": SUBTRACT_PHONE},
        )
        await session.execute(text("DELETE FROM patients WHERE phone_e164 = :p"), {"p": SUBTRACT_PHONE})
        await session.commit()


# ── normalize_phone ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919000000901", "+919000000901"),
        ("9876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+1 (628) 356-4436", "+16283564436"),
        ("98765 43210", "+919876543210"),
        ("", ""),
        (None, ""),
        ("hello", ""),
        ("98+76543210", ""),  # stray '+' mid-string must not become identity
        ("+12", ""),  # absurdly short
        ("1234", ""),  # too short to be a number
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


# ── slot filter boundaries ─────────────────────────────────────────────────

def _utc_at_ist(hour: int, minute: int = 0) -> datetime:
    local = timeutils.now_local().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def test_slot_filter_boundaries():
    noon = _utc_at_ist(12, 0)
    assert timeutils.slot_matches_filters(noon, part_of_day=["afternoon"])
    assert not timeutils.slot_matches_filters(noon, part_of_day=["morning"])
    five_pm = _utc_at_ist(17, 0)
    assert timeutils.slot_matches_filters(five_pm, part_of_day=["evening"])
    assert not timeutils.slot_matches_filters(five_pm, part_of_day=["afternoon"])
    weekday = timeutils.WEEKDAYS[timeutils.utc_to_local(noon).weekday()]
    assert timeutils.slot_matches_filters(noon, weekday_mask=[weekday])
    other = timeutils.WEEKDAYS[(timeutils.utc_to_local(noon).weekday() + 1) % 7]
    assert not timeutils.slot_matches_filters(noon, weekday_mask=[other])


# ── idempotency key semantics ─────────────────────────────────────────────

def test_idempotency_key_ignores_execution_message():
    base = {"slot_id": "abc", "patient_full_name": "A B"}
    with_filler = {**base, "execution_message": "Ek moment..."}
    other_filler = {**base, "execution_message": "One moment please."}
    assert _idempotency_key("c1", "book_appointment", base) == _idempotency_key(
        "c1", "book_appointment", with_filler
    )
    assert _idempotency_key("c1", "book_appointment", with_filler) == _idempotency_key(
        "c1", "book_appointment", other_filler
    )
    # but real argument changes must change the key
    assert _idempotency_key("c1", "book_appointment", base) != _idempotency_key(
        "c1", "book_appointment", {**base, "slot_id": "xyz"}
    )
    # and different conversations must never share keys
    assert _idempotency_key("c1", "book_appointment", base) != _idempotency_key(
        "c2", "book_appointment", base
    )


# ── upsert_session concurrency ────────────────────────────────────────────

async def test_upsert_session_concurrent_first_write():
    from app.services import sessions as sessions_svc

    async def write(key: str):
        async with SessionLocal() as session:
            await sessions_svc.upsert_session(
                session, RACE_CALL_ID, "+919000000980", stage="in_task", collected={key: "1"}
            )
            await session.commit()

    await asyncio.gather(write("alpha"), write("beta"))
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT collected FROM call_sessions WHERE call_id = :c"), {"c": RACE_CALL_ID}
            )
        ).first()
    assert row is not None
    assert set(row.collected) >= {"alpha", "beta"}


# ── endpoint authentication ───────────────────────────────────────────────

def _client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_tool_endpoint_rejects_unauthenticated():
    async with _client() as client:
        body = {"name": "get_patient_record", "call": {}, "args": {}}
        no_auth = await client.post("/tools/get_patient_record", json=body)
        assert no_auth.status_code == 401
        bad_secret = await client.post(
            "/tools/get_patient_record", json=body, headers={"X-Tool-Secret": "wrong"}
        )
        assert bad_secret.status_code == 401
        bad_signature = await client.post(
            "/tools/get_patient_record", json=body, headers={"X-Retell-Signature": "v=1,d=bogus"}
        )
        assert bad_signature.status_code == 401


async def test_tool_endpoint_accepts_shared_secret():
    assert settings.tool_shared_secret, "TOOL_SHARED_SECRET must be configured for this test"
    async with _client() as client:
        response = await client.post(
            "/tools/get_patient_record",
            json={"name": "get_patient_record", "call": {}, "args": {"patient_phone": "+919000000999"}},
            headers={"X-Tool-Secret": settings.tool_shared_secret},
        )
    assert response.status_code == 200
    assert response.json()["status"] in {"new_patient", "found"}


# ── webhook idempotency ───────────────────────────────────────────────────

async def test_call_ended_webhook_is_idempotent():
    # Pre-complete the session so the handler takes the no-summarization path.
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO call_sessions (id, call_id, phone_e164, stage) "
                "VALUES (:id, :c, :p, 'completed') ON CONFLICT (call_id) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "c": WEBHOOK_CALL_ID, "p": WEBHOOK_PHONE},
        )
        await session.commit()

    payload = {
        "event": "call_ended",
        "call": {
            "call_id": WEBHOOK_CALL_ID,
            "from_number": WEBHOOK_PHONE,
            "direction": "inbound",
            "disconnection_reason": "user_hangup",
            "transcript": "test",
        },
    }
    async with _client() as client:
        first = await client.post(
            "/retell/webhook", json=payload, headers={"X-Tool-Secret": settings.tool_shared_secret}
        )
        second = await client.post(
            "/retell/webhook", json=payload, headers={"X-Tool-Secret": settings.tool_shared_secret}
        )
    assert first.status_code == 200 and second.status_code == 200
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT status FROM call_log WHERE call_id = :c"), {"c": WEBHOOK_CALL_ID}
            )
        ).all()
    assert len(rows) == 1 and rows[0].status == "ended"


# ── availability local-subtraction ────────────────────────────────────────

async def test_local_booking_subtracted_from_cliniko_slots(monkeypatch):
    async with SessionLocal() as session:
        refs = (
            await session.execute(
                text(
                    "SELECT pr.id AS practitioner_id, b.id AS branch_id, t.id AS type_id "
                    "FROM practitioners pr "
                    "JOIN practitioner_branches pb ON pb.practitioner_id = pr.id "
                    "JOIN branches b ON b.id = pb.branch_id, appointment_types t LIMIT 1"
                )
            )
        ).first()
        practitioner = await session.execute(
            text("SELECT id, name FROM practitioners WHERE id = :id"), {"id": refs.practitioner_id}
        )
        practitioner = practitioner.first()

    slot_start = (timeutils.now_local() + timedelta(days=40)).replace(
        hour=11, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)

    from app.db.models import AppointmentType, Branch, Practitioner

    async with SessionLocal() as session:
        combo = availability.SlotCombo(
            practitioner=await session.get(Practitioner, refs.practitioner_id),
            branch=await session.get(Branch, refs.branch_id),
            appointment_type=await session.get(AppointmentType, refs.type_id),
        )

        async def fake_resolve(*args, **kwargs):
            return [combo]

        async def fake_fetch(c, f, t, bypass_cache=False):
            return [slot_start.strftime("%Y-%m-%dT%H:%M:%SZ")]

        monkeypatch.setattr(availability, "resolve_combos", fake_resolve)
        monkeypatch.setattr(availability, "fetch_combo_times", fake_fetch)

        before = await availability.search_slots(
            session, date_from=timeutils.utc_to_local(slot_start).date(),
            date_to=timeutils.utc_to_local(slot_start).date(),
        )
        assert len(before["slots"]) == 1, "slot should be offered while unbooked"

        # Book it locally (no Cliniko) — the slot must vanish from results.
        patient_id = uuid.uuid4()
        await session.execute(
            text("INSERT INTO patients (id, full_name, phone_e164) VALUES (:id, 'Sub Tract', :p)"),
            {"id": patient_id, "p": SUBTRACT_PHONE},
        )
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                "appointment_type_id, during, status, fee_inr, cliniko_sync_status) VALUES "
                "(:id, :pid, :prid, :bid, :tid, tstzrange(:s, :e, '[)'), 'confirmed', 400, 'pending')"
            ),
            {
                "id": uuid.uuid4(), "pid": patient_id, "prid": refs.practitioner_id,
                "bid": refs.branch_id, "tid": refs.type_id,
                "s": slot_start, "e": slot_start + timedelta(minutes=45),
            },
        )
        await session.commit()

        after = await availability.search_slots(
            session, date_from=timeutils.utc_to_local(slot_start).date(),
            date_to=timeutils.utc_to_local(slot_start).date(),
        )
        assert after["slots"] == [], "locally-booked slot must be subtracted even before Cliniko sync"
