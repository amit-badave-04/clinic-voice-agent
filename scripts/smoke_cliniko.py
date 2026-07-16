"""Cliniko smoke test: prints tomorrow's open slots for every
practitioner × branch × the initial-assessment type.
Run: python -m scripts.smoke_cliniko"""
import asyncio
from datetime import timedelta

from app.db.session import SessionLocal
from app.services import availability, timeutils


async def main() -> None:
    tomorrow = timeutils.today_local() + timedelta(days=1)
    async with SessionLocal() as session:
        combos = await availability.resolve_combos(session, "any", None, "initial")
        if not combos:
            print("No combos found — run seed.cliniko_seed first.")
            return
        print(f"Slots for {tomorrow} (initial assessment):\n")
        for combo in combos:
            times = await availability._fetch_combo_times(combo, tomorrow, tomorrow)
            label = f"{combo.practitioner.name} @ {combo.branch.key}"
            if times:
                local = [
                    timeutils.speakable_datetime(
                        __import__("datetime").datetime.fromisoformat(t.replace("Z", "+00:00"))
                    )
                    for t in times[:4]
                ]
                print(f"  {label}: {len(times)} slots — first: {local}")
            else:
                print(f"  {label}: NO SLOTS (check schedule + online-booking flags)")


if __name__ == "__main__":
    asyncio.run(main())
