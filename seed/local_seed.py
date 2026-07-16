"""Local-only seed: demo patients (incl. the family-shared-phone pair) and
clinic policies. Idempotent. Run: python -m seed.local_seed
(Branches/practitioners/appointment types are seeded by seed.cliniko_seed,
which also maps Cliniko IDs.)"""
import asyncio
import uuid

from sqlalchemy import text

from app.db.session import SessionLocal
from seed import arogya_data


async def main() -> None:
    async with SessionLocal() as session:
        for patient in arogya_data.DEMO_PATIENTS:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM patients WHERE phone_e164 = :phone AND full_name = :name"
                    ),
                    {"phone": patient["phone_e164"], "name": patient["full_name"]},
                )
            ).first()
            if not row:
                await session.execute(
                    text(
                        "INSERT INTO patients (id, full_name, phone_e164, preferred_branch, notes) "
                        "VALUES (:id, :name, :phone, :branch, 'demo seed patient')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "name": patient["full_name"],
                        "phone": patient["phone_e164"],
                        "branch": patient["preferred_branch"],
                    },
                )
                print(f"  patient created: {patient['full_name']} ({patient['phone_e164']})")
        for key, value in arogya_data.CLINIC_POLICIES.items():
            await session.execute(
                text(
                    "INSERT INTO clinic_policies (key, value) VALUES (:key, :value) "
                    "ON CONFLICT (key) DO UPDATE SET value = :value"
                ),
                {"key": key, "value": value},
            )
        await session.commit()
    print("local seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
