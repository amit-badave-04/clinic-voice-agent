"""Direct-DB setup and truth-checking for eval scenarios.

The DB is the source of truth, so scenario outcomes are asserted here — a
booking either exists or it doesn't, regardless of what the transcript claims.
Seeded appointments are local-only (no Cliniko id) so cleanup never has to
touch the PMS for fixtures; bookings the agent creates during a scenario DO
write to Cliniko and are cancelled through the real cancel path."""
import uuid
from datetime import timedelta

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import booking, timeutils
from evals.common import EVAL_PHONE_PREFIX


async def seed_patient(phone: str, full_name: str) -> None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id FROM patients WHERE phone_e164 = :p AND full_name = :n"),
                {"p": phone, "n": full_name},
            )
        ).first()
        if not row:
            await session.execute(
                text(
                    "INSERT INTO patients (id, full_name, phone_e164, notes) "
                    "VALUES (:id, :n, :p, 'eval fixture')"
                ),
                {"id": uuid.uuid4(), "n": full_name, "p": phone},
            )
        await session.commit()


async def seed_appointment(phone: str, full_name: str, hours_from_now: float, minutes: int = 45) -> str:
    """Local-only confirmed appointment (no Cliniko id). Returns appointment_id."""
    await seed_patient(phone, full_name)
    start = timeutils.now_utc() + timedelta(hours=hours_from_now)
    appt_id = uuid.uuid4()
    async with SessionLocal() as session:
        ref = (
            await session.execute(
                text(
                    "SELECT p.id AS patient_id, pr.id AS practitioner_id, b.id AS branch_id, t.id AS type_id "
                    "FROM patients p, practitioners pr "
                    "JOIN practitioner_branches pb ON pb.practitioner_id = pr.id "
                    "JOIN branches b ON b.id = pb.branch_id, appointment_types t "
                    "WHERE p.phone_e164 = :p AND p.full_name = :n LIMIT 1"
                ),
                {"p": phone, "n": full_name},
            )
        ).first()
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, appointment_type_id, "
                "during, status, fee_inr, cliniko_sync_status) VALUES "
                "(:id, :pid, :prid, :bid, :tid, tstzrange(:s, :e, '[)'), 'confirmed', 400, 'synced')"
            ),
            {
                "id": appt_id,
                "pid": ref.patient_id,
                "prid": ref.practitioner_id,
                "bid": ref.branch_id,
                "tid": ref.type_id,
                "s": start,
                "e": start + timedelta(minutes=minutes),
            },
        )
        await session.commit()
    return str(appt_id)


async def confirmed_count(phone: str) -> int:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS c FROM appointments a JOIN patients p ON p.id = a.patient_id "
                    "WHERE p.phone_e164 = :p AND a.status = 'confirmed'"
                ),
                {"p": phone},
            )
        ).first()
        return int(row.c)


async def followup_ticket_count_by_reason(keyword: str) -> int:
    """Recent tickets matching a reason keyword — used when the channel can't
    attribute caller identity (chat evals)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS c FROM followup_tickets "
                    "WHERE reason ILIKE :kw AND created_at > now() - interval '30 minutes'"
                ),
                {"kw": f"%{keyword}%"},
            )
        ).first()
        return int(row.c)


async def followup_ticket_count(phone: str) -> int:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT count(*) AS c FROM followup_tickets WHERE phone_e164 = :p"), {"p": phone}
            )
        ).first()
        return int(row.c)


async def cleanup_eval_data() -> dict:
    """Cancel every confirmed appointment on eval phones (through the real
    cancel path so Cliniko is cleaned too), then remove fixture rows."""
    cancelled = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id "
                    "WHERE p.phone_e164 LIKE :prefix AND a.status = 'confirmed'"
                ),
                {"prefix": f"{EVAL_PHONE_PREFIX}%"},
            )
        ).all()
    for row in rows:
        async with SessionLocal() as session:
            result = await booking.cancel(
                session,
                phone_e164="",  # appointment_id path ignores phone when blank
                patient_name=None,
                idempotency_key=f"eval-cleanup-{row.id}",
                appointment_id=str(row.id),
            )
            if result.get("status") == "cancelled":
                cancelled += 1
    async with SessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM call_sessions WHERE phone_e164 LIKE :prefix"
            ),
            {"prefix": f"{EVAL_PHONE_PREFIX}%"},
        )
        await session.execute(
            text("DELETE FROM pending_callbacks WHERE phone_e164 LIKE :prefix"),
            {"prefix": f"{EVAL_PHONE_PREFIX}%"},
        )
        await session.commit()
    return {"cancelled": cancelled}
