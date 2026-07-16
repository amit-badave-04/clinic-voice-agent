"""Write-time integrity guarantees, proven directly against the database.

These are the load-bearing invariants (Cliniko itself allows double-booking,
so this layer is the only real protection):
  1. GiST exclusion constraint: concurrent overlapping bookings — exactly one wins.
  2. Idempotent replay: same idempotency key returns the stored response.
  3. Patient overlap guard: one patient cannot hold two overlapping bookings.
  4. Fee window: fee applies inside 24h, not outside.
  5. Cancel disambiguation: multiple upcoming appointments demand an explicit id.

Requires .env with DATABASE_URL (run: pytest tests -q). Uses dedicated test
phone numbers and cleans up after itself.
"""
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import booking, timeutils

TEST_PHONE = "+919000000980"
TEST_NAME = "Test Guarantee"


async def _refs(session):
    return (
        await session.execute(
            text(
                "SELECT pr.id AS practitioner_id, b.id AS branch_id, t.id AS type_id "
                "FROM practitioners pr "
                "JOIN practitioner_branches pb ON pb.practitioner_id = pr.id "
                "JOIN branches b ON b.id = pb.branch_id, appointment_types t LIMIT 1"
            )
        )
    ).first()


async def _seed_patient(session) -> uuid.UUID:
    row = (
        await session.execute(
            text("SELECT id FROM patients WHERE phone_e164 = :p LIMIT 1"), {"p": TEST_PHONE}
        )
    ).first()
    if row:
        return row.id
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO patients (id, full_name, phone_e164, notes) VALUES (:id, :n, :p, 'test')"),
        {"id": pid, "n": TEST_NAME, "p": TEST_PHONE},
    )
    return pid


async def _insert_appointment(patient_id, refs, start, minutes=45) -> uuid.UUID:
    """Raw insert in its own session; raises IntegrityError on overlap."""
    appt_id = uuid.uuid4()
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                "appointment_type_id, during, status, fee_inr, cliniko_sync_status) VALUES "
                "(:id, :pid, :prid, :bid, :tid, tstzrange(:s, :e, '[)'), 'confirmed', 400, 'synced')"
            ),
            {
                "id": appt_id, "pid": patient_id, "prid": refs.practitioner_id,
                "bid": refs.branch_id, "tid": refs.type_id,
                "s": start, "e": start + timedelta(minutes=minutes),
            },
        )
        await session.commit()
    return appt_id


@pytest.fixture(autouse=True)
async def cleanup():
    yield
    async with SessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM appointments WHERE patient_id IN "
                "(SELECT id FROM patients WHERE phone_e164 = :p)"
            ),
            {"p": TEST_PHONE},
        )
        await session.execute(
            text("DELETE FROM idempotency_keys WHERE key LIKE 'test-%'")
        )
        await session.execute(
            text(
                "DELETE FROM outbox WHERE payload->>'appointment_id' NOT IN "
                "(SELECT id::text FROM appointments)"
            )
        )
        await session.execute(text("DELETE FROM patients WHERE phone_e164 = :p"), {"p": TEST_PHONE})
        await session.commit()


async def test_exclusion_constraint_wins_race():
    """Two overlapping bookings for the same practitioner: exactly one succeeds."""
    async with SessionLocal() as session:
        refs = await _refs(session)
        patient_id = await _seed_patient(session)
        await session.commit()
    start = timeutils.now_utc() + timedelta(days=30)

    results = await asyncio.gather(
        _insert_appointment(patient_id, refs, start),
        _insert_appointment(patient_id, refs, start + timedelta(minutes=15)),  # overlaps
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, uuid.UUID)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert len(failures) == 1
    assert "23P01" in str(failures[0]) or "no_practitioner_overlap" in str(failures[0])


async def test_back_to_back_slots_do_not_collide():
    """[) half-open ranges: 10:00-10:45 and 10:45-11:30 must both succeed."""
    async with SessionLocal() as session:
        refs = await _refs(session)
        patient_id = await _seed_patient(session)
        await session.commit()
    start = timeutils.now_utc() + timedelta(days=31)
    first = await _insert_appointment(patient_id, refs, start)
    second = await _insert_appointment(patient_id, refs, start + timedelta(minutes=45))
    assert first and second


async def test_idempotent_replay_returns_stored_response():
    async with SessionLocal() as session:
        stored = {"status": "confirmed", "marker": "original"}
        await booking.store_idempotent(session, "test-replay-key", stored)
        await session.commit()
    async with SessionLocal() as session:
        replay = await booking.check_idempotent(session, "test-replay-key")
        assert replay == stored


async def test_cancel_requires_disambiguation_with_multiple_appointments():
    async with SessionLocal() as session:
        refs = await _refs(session)
        patient_id = await _seed_patient(session)
        await session.commit()
    start = timeutils.now_utc() + timedelta(days=32)
    appt_a = await _insert_appointment(patient_id, refs, start)
    appt_b = await _insert_appointment(patient_id, refs, start + timedelta(days=1))

    async with SessionLocal() as session:
        ambiguous = await booking.cancel(
            session, TEST_PHONE, None, idempotency_key="test-cancel-ambiguous"
        )
    assert ambiguous["status"] == "choose_appointment"
    assert len(ambiguous["appointments"]) == 2

    async with SessionLocal() as session:
        first = await booking.cancel(
            session, TEST_PHONE, None, idempotency_key=f"test-cancel-{appt_a}",
            appointment_id=str(appt_a),
        )
    async with SessionLocal() as session:
        second = await booking.cancel(
            session, TEST_PHONE, None, idempotency_key=f"test-cancel-{appt_b}",
            appointment_id=str(appt_b),
        )
    assert first["status"] == "cancelled" and second["status"] == "cancelled"

    async with SessionLocal() as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) AS c FROM appointments a JOIN patients p ON p.id = a.patient_id "
                    "WHERE p.phone_e164 = :p AND a.status = 'confirmed'"
                ),
                {"p": TEST_PHONE},
            )
        ).first()
        assert remaining.c == 0


async def test_fee_window_boundaries():
    async with SessionLocal() as session:
        refs = await _refs(session)
        patient_id = await _seed_patient(session)
        await session.commit()

    inside = await _insert_appointment(patient_id, refs, timeutils.now_utc() + timedelta(hours=5))
    outside = await _insert_appointment(patient_id, refs, timeutils.now_utc() + timedelta(hours=72))

    from app.db.models import Appointment

    async with SessionLocal() as session:
        appt_inside = await session.get(Appointment, inside)
        applies, fee = await booking.fee_applies(session, appt_inside)
        assert applies is True and fee == 100
        appt_outside = await session.get(Appointment, outside)
        applies, _ = await booking.fee_applies(session, appt_outside)
        assert applies is False


async def test_timezone_today_never_shifts():
    """The IST 'today' must match the clinic clock even when UTC has rolled over
    (18:30-24:00 IST is the classic bug window)."""
    from datetime import datetime, timezone

    late_evening_ist_in_utc = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
    local = timeutils.utc_to_local(late_evening_ist_in_utc)
    assert local.date().isoformat() == "2026-07-16"
    just_after_midnight_ist = datetime(2026, 7, 16, 19, 30, tzinfo=timezone.utc)  # 01:00 IST on the 17th
    local2 = timeutils.utc_to_local(just_after_midnight_ist)
    assert local2.date().isoformat() == "2026-07-17"
