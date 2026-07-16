"""Cliniko seeder / synchronizer (idempotent — safe to re-run).

Run:  python -m seed.cliniko_seed

What it does automatically (API-supported):
  1. Ensures both branches exist as Cliniko Businesses (creates missing ones).
  2. Ensures all appointment types exist (creates missing; Cliniko duration =
     consult duration + buffer, so buffers survive round-trips).
  3. Reads practitioners from Cliniko and matches them to the roster by name.
  4. Upserts everything into the local Postgres mirror (branches, practitioners,
     practitioner_branches, appointment_types, clinic_policies, demo patients).

What Cliniko's API does NOT allow (must be done once in the Cliniko UI —
this script prints a personalized checklist of exactly what's missing):
  - Creating practitioners (they are user accounts): Settings → Users &
    practitioners → Add user → mark as practitioner.
  - Setting practitioner appointment schedules per branch (working hours).
  - Enabling online bookings for business/practitioner/appointment type
    (required — available_times returns nothing otherwise).

See SETUP_CLINIKO.md for the full step-by-step guide.
"""
import asyncio
import sys
import uuid

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.cliniko import ClinikoError, get_cliniko
from seed import arogya_data


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("dr.", "").replace("dr ", "").split())


async def ensure_businesses(cliniko) -> dict[str, str]:
    """Returns {branch_key: cliniko_business_id}."""
    existing = await cliniko.list_businesses()
    by_name = {b["business_name"].strip().lower(): b for b in existing if b.get("business_name")}
    mapping = {}
    for branch in arogya_data.BRANCHES:
        found = by_name.get(branch["name"].strip().lower())
        if not found:
            # fuzzy: match on key fragment (e.g. "medax", "arc")
            found = next(
                (b for name, b in by_name.items() if branch["key"] in name), None
            )
        if found:
            mapping[branch["key"]] = str(found["id"])
            print(f"  business exists: {branch['name']} (id {found['id']})")
        else:
            created = await cliniko.create_business(branch["name"], branch["address"])
            mapping[branch["key"]] = str(created["id"])
            print(f"  business CREATED: {branch['name']} (id {created['id']})")
    return mapping


async def ensure_appointment_types(cliniko) -> dict[str, str]:
    existing = await cliniko.list_appointment_types()
    by_name = {t["name"].strip().lower(): t for t in existing if t.get("name")}
    # Cliniko only accepts colors from its own palette; borrow one from an
    # existing type (the trial's defaults are valid).
    valid_color = next((t["color"] for t in existing if t.get("color")), "#B8D9FF")
    mapping = {}
    for appt in arogya_data.APPOINTMENT_TYPES:
        found = by_name.get(appt["name"].strip().lower())
        cliniko_duration = appt["duration_minutes"] + appt["buffer_minutes"]
        if found:
            mapping[appt["key"]] = str(found["id"])
            print(f"  appointment type exists: {appt['name']} (id {found['id']})")
        else:
            created = await cliniko.create_appointment_type(appt["name"], cliniko_duration, valid_color)
            mapping[appt["key"]] = str(created["id"])
            print(f"  appointment type CREATED: {appt['name']} ({cliniko_duration} min incl. buffer)")
    return mapping


async def match_practitioners(cliniko) -> tuple[dict[str, str], list[str]]:
    """Returns ({roster_name: cliniko_practitioner_id}, [missing roster names])."""
    existing = await cliniko.list_practitioners()
    matched, missing = {}, []
    for practitioner in arogya_data.ENABLED_PRACTITIONERS:
        target = _norm(practitioner["name"])
        hit = None
        for p in existing:
            display = p.get("display_name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}"
            candidate = _norm(display)
            if not candidate:  # empty name must never wildcard-match
                continue
            if candidate == target:
                hit = p
                break
            # substring only for real names (the trial owner "A B" must not match)
            if len(candidate) >= 5 and (target in candidate or candidate in target):
                hit = p
                break
            if len(candidate.split()[-1]) >= 4 and target.split()[-1] == candidate.split()[-1]:
                hit = p
                break
        if hit:
            matched[practitioner["name"]] = str(hit["id"])
            print(f"  practitioner matched: {practitioner['name']} -> id {hit['id']}")
        else:
            missing.append(practitioner["name"])
    return matched, missing


async def upsert_local(business_map: dict, type_map: dict, practitioner_map: dict) -> None:
    async with SessionLocal() as session:
        branch_ids = {}
        for branch in arogya_data.BRANCHES:
            row = (
                await session.execute(
                    text("SELECT id FROM branches WHERE key = :key"), {"key": branch["key"]}
                )
            ).first()
            if row:
                branch_ids[branch["key"]] = row.id
                await session.execute(
                    text("UPDATE branches SET cliniko_business_id = :cid WHERE key = :key"),
                    {"cid": business_map.get(branch["key"]), "key": branch["key"]},
                )
            else:
                new_id = uuid.uuid4()
                branch_ids[branch["key"]] = new_id
                await session.execute(
                    text(
                        "INSERT INTO branches (id, key, name, address, cliniko_business_id, timezone) "
                        "VALUES (:id, :key, :name, :addr, :cid, :tz)"
                    ),
                    {
                        "id": new_id,
                        "key": branch["key"],
                        "name": branch["name"],
                        "addr": branch["address"],
                        "cid": business_map.get(branch["key"]),
                        "tz": branch["timezone"],
                    },
                )

        for appt in arogya_data.APPOINTMENT_TYPES:
            row = (
                await session.execute(
                    text("SELECT id FROM appointment_types WHERE key = :key"), {"key": appt["key"]}
                )
            ).first()
            if row:
                await session.execute(
                    text("UPDATE appointment_types SET cliniko_appointment_type_id = :cid WHERE key = :key"),
                    {"cid": type_map.get(appt["key"]), "key": appt["key"]},
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO appointment_types (id, key, name, duration_minutes, buffer_minutes, fee_inr, cliniko_appointment_type_id) "
                        "VALUES (:id, :key, :name, :dur, :buf, :fee, :cid)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "key": appt["key"],
                        "name": appt["name"],
                        "dur": appt["duration_minutes"],
                        "buf": appt["buffer_minutes"],
                        "fee": appt["fee_inr"],
                        "cid": type_map.get(appt["key"]),
                    },
                )

        import json as json_mod

        for practitioner in arogya_data.ENABLED_PRACTITIONERS:
            row = (
                await session.execute(
                    text("SELECT id FROM practitioners WHERE name = :name"),
                    {"name": practitioner["name"]},
                )
            ).first()
            if row:
                practitioner_id = row.id
                await session.execute(
                    text("UPDATE practitioners SET cliniko_practitioner_id = :cid WHERE id = :id"),
                    {"cid": practitioner_map.get(practitioner["name"]), "id": practitioner_id},
                )
            else:
                practitioner_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO practitioners (id, name, specialties, cliniko_practitioner_id) "
                        "VALUES (:id, :name, CAST(:spec AS jsonb), :cid)"
                    ),
                    {
                        "id": practitioner_id,
                        "name": practitioner["name"],
                        "spec": json_mod.dumps(practitioner["specialties"]),
                        "cid": practitioner_map.get(practitioner["name"]),
                    },
                )
            for branch_key in practitioner["schedule"]:
                await session.execute(
                    text(
                        "INSERT INTO practitioner_branches (practitioner_id, branch_id) "
                        "VALUES (:pid, :bid) ON CONFLICT DO NOTHING"
                    ),
                    {"pid": practitioner_id, "bid": branch_ids[branch_key]},
                )

        # Remove roster members not enabled on this trial (5-active-practitioner
        # cap) so availability fan-out never queries them.
        disabled = [p["name"] for p in arogya_data.PRACTITIONERS if not p["enabled"]]
        if disabled:
            rows = (
                await session.execute(
                    text("SELECT id, name FROM practitioners WHERE name = ANY(:names)"),
                    {"names": disabled},
                )
            ).all()
            for row in rows:
                try:
                    await session.execute(
                        text("DELETE FROM practitioner_branches WHERE practitioner_id = :id"),
                        {"id": row.id},
                    )
                    await session.execute(
                        text("DELETE FROM practitioners WHERE id = :id"), {"id": row.id}
                    )
                    print(f"  removed disabled practitioner from local mirror: {row.name}")
                except Exception as exc:  # noqa: BLE001 — has appointments; keep but unlink
                    print(f"  could not remove {row.name} (has data?): {exc}")

        for key, value in arogya_data.CLINIC_POLICIES.items():
            await session.execute(
                text(
                    "INSERT INTO clinic_policies (key, value) VALUES (:key, :value) "
                    "ON CONFLICT (key) DO UPDATE SET value = :value"
                ),
                {"key": key, "value": value},
            )

        await session.commit()
    print("  local mirror updated.")


async def main() -> None:
    cliniko = get_cliniko()
    print("1) Businesses (branches):")
    try:
        business_map = await ensure_businesses(cliniko)
    except ClinikoError as exc:
        print(f"FATAL: Cliniko API not reachable/authorized: {exc}")
        print("Check CLINIKO_API_KEY in .env and that the key's user has Administrator + Practitioner roles.")
        sys.exit(1)

    print("2) Appointment types:")
    type_map = await ensure_appointment_types(cliniko)

    print("3) Practitioners (API is read-only for these):")
    practitioner_map, missing = await match_practitioners(cliniko)

    print("4) Local Postgres mirror:")
    await upsert_local(business_map, type_map, practitioner_map)

    if missing:
        print("\n" + "=" * 72)
        print("MANUAL CLINIKO STEPS REQUIRED — missing practitioners:")
        for name in missing:
            schedule = next(p["schedule"] for p in arogya_data.ENABLED_PRACTITIONERS if p["name"] == name)
            print(f"  - {name}: add via Settings → Users & practitioners (any unique email),")
            for branch_key, blocks in schedule.items():
                branch_name = next(b["name"] for b in arogya_data.BRANCHES if b["key"] == branch_key)
                blocks_str = ", ".join(f"{s}-{e}" for s, e in blocks)
                print(f"      schedule at {branch_name}: Mon-Sat {blocks_str}")
        print("Then re-run this script. Full guide: SETUP_CLINIKO.md")
        print("=" * 72)
    else:
        print("\nAll practitioners matched. Verify online bookings are enabled for every")
        print("business + practitioner + appointment type (SETUP_CLINIKO.md step 5),")
        print("otherwise Cliniko's available_times returns nothing.")


if __name__ == "__main__":
    asyncio.run(main())
