"""Minimal Cliniko-free seed for CI: just enough reference rows (one branch,
one practitioner, one appointment type, policies) for the DB guarantee tests.
Idempotent. Run: python -m seed.ci_seed"""
import asyncio
import uuid

from sqlalchemy import text

from app.db.session import SessionLocal


async def main() -> None:
    async with SessionLocal() as session:
        branch = (await session.execute(text("SELECT id FROM branches WHERE key = 'ci'"))).first()
        if not branch:
            branch_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO branches (id, key, name, address) "
                    "VALUES (:id, 'ci', 'CI Test Branch', 'nowhere')"
                ),
                {"id": branch_id},
            )
        else:
            branch_id = branch.id

        practitioner = (
            await session.execute(text("SELECT id FROM practitioners WHERE name = 'Dr. CI Test'"))
        ).first()
        if not practitioner:
            practitioner_id = uuid.uuid4()
            await session.execute(
                text("INSERT INTO practitioners (id, name) VALUES (:id, 'Dr. CI Test')"),
                {"id": practitioner_id},
            )
        else:
            practitioner_id = practitioner.id

        await session.execute(
            text(
                "INSERT INTO practitioner_branches (practitioner_id, branch_id) "
                "VALUES (:p, :b) ON CONFLICT DO NOTHING"
            ),
            {"p": practitioner_id, "b": branch_id},
        )
        appt_type = (
            await session.execute(text("SELECT id FROM appointment_types WHERE key = 'ci_type'"))
        ).first()
        if not appt_type:
            await session.execute(
                text(
                    "INSERT INTO appointment_types (id, key, name, duration_minutes, buffer_minutes, fee_inr) "
                    "VALUES (:id, 'ci_type', 'CI Consultation', 40, 5, 400)"
                ),
                {"id": uuid.uuid4()},
            )
        for key, value in (("change_fee_window_hours", "24"), ("change_fee_inr", "100")):
            await session.execute(
                text(
                    "INSERT INTO clinic_policies (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = :v"
                ),
                {"k": key, "v": value},
            )
        await session.commit()
    print("ci seed complete")


if __name__ == "__main__":
    asyncio.run(main())
