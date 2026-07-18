"""Two-way drift reconciliation between local Postgres and Cliniko.

Staff work directly in Cliniko (which has no webhooks), the agent writes
through the outbox — so the two calendars can drift. Every cycle compares a
rolling window on both sides and sorts differences into buckets:

  auto-healed (safe, no human):
    - cliniko_only:  staff created an appointment directly in Cliniko →
      mirror it locally (source_system='cliniko') so the exclusion constraint
      and availability logic see it and the agent can't double-book that slot.
    - time_mismatch: staff moved an agent-created appointment in Cliniko →
      Cliniko wins for staff edits; local time is updated and the row marked
      externally_modified. If the new time collides locally, it becomes a
      ticket instead.

  human tickets (never auto-resolved):
    - missing_in_cliniko: a local confirmed appointment whose Cliniko copy is
      cancelled or gone — staff intent is unclear, cancelling locally on a
      guess could strand a patient.

  left to the outbox (its own retry/alert path):
    - local rows still pending sync.

Demo-scale note: the dataset is dozens of rows, so each cycle does a FULL
window comparison — simpler and strictly more correct than updated_at cursors.
At production volume this would switch to incremental `q[]=updated_at:>` pulls
with a periodic full sweep; the bucket logic below would not change.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal
from app.services import alerts, timeutils
from app.services.cliniko import get_cliniko

log = logging.getLogger("reconcile")
settings = get_settings()

WINDOW_BACK_DAYS = 1
WINDOW_AHEAD_DAYS = 60


@dataclass
class Drift:
    cliniko_only: list[dict] = field(default_factory=list)
    time_mismatch: list[tuple[dict, dict]] = field(default_factory=list)  # (local, remote)
    missing_in_cliniko: list[dict] = field(default_factory=list)


def diff_calendars(local: list[dict], remote: list[dict]) -> Drift:
    """Pure bucket logic. `local` rows: {id, cliniko_appointment_id, starts_at
    (aware UTC), status}. `remote` rows: raw Cliniko appointments (id,
    starts_at ISO, cancelled_at)."""
    drift = Drift()
    remote_by_id = {str(r["id"]): r for r in remote}
    local_by_cliniko_id = {
        str(l["cliniko_appointment_id"]): l for l in local if l["cliniko_appointment_id"]
    }

    for l in local:
        if l["status"] != "confirmed" or not l["cliniko_appointment_id"]:
            continue  # unsynced rows belong to the outbox, not reconciliation
        r = remote_by_id.get(str(l["cliniko_appointment_id"]))
        if r is None or r.get("cancelled_at"):
            drift.missing_in_cliniko.append(l)
        else:
            remote_start = _parse_utc(r["starts_at"])
            if remote_start != l["starts_at"]:
                drift.time_mismatch.append((l, r))

    for r in remote:
        if r.get("cancelled_at"):
            continue
        if str(r["id"]) not in local_by_cliniko_id:
            drift.cliniko_only.append(r)
    return drift


def _parse_utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _id_from_link(record: dict, key: str) -> str:
    """Cliniko nests references as {'key': {'links': {'self': '.../123'}}}."""
    link = ((record.get(key) or {}).get("links") or {}).get("self", "")
    return link.rstrip("/").rsplit("/", 1)[-1] if link else ""


async def _load_local(session) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT a.id, a.cliniko_appointment_id, a.status, lower(a.during) AS starts_at "
                "FROM appointments a "
                "WHERE lower(a.during) >= now() - (:back || ' days')::interval "
                "AND lower(a.during) < now() + (:ahead || ' days')::interval"
            ),
            {"back": WINDOW_BACK_DAYS, "ahead": WINDOW_AHEAD_DAYS},
        )
    ).all()
    return [
        {
            "id": r.id,
            "cliniko_appointment_id": r.cliniko_appointment_id,
            "status": r.status,
            "starts_at": r.starts_at.astimezone(timezone.utc),
        }
        for r in rows
    ]


async def _mirror_cliniko_appointment(session, remote: dict) -> bool:
    """Insert a local mirror of a staff-created Cliniko appointment. Returns
    False (→ ticket) when any reference can't be mapped."""
    practitioner_id = _id_from_link(remote, "practitioner")
    business_id = _id_from_link(remote, "business")
    type_id = _id_from_link(remote, "appointment_type")
    patient_id = _id_from_link(remote, "patient")
    refs = (
        await session.execute(
            text(
                "SELECT (SELECT id FROM practitioners WHERE cliniko_practitioner_id = :pr) AS practitioner_id, "
                "(SELECT id FROM branches WHERE cliniko_business_id = :b) AS branch_id, "
                "(SELECT id FROM appointment_types WHERE cliniko_appointment_type_id = :t) AS type_id"
            ),
            {"pr": practitioner_id, "b": business_id, "t": type_id},
        )
    ).first()
    if not (refs and refs.practitioner_id and refs.branch_id and refs.type_id):
        return False

    local_patient = (
        await session.execute(
            text("SELECT id FROM patients WHERE cliniko_patient_id = :cp"), {"cp": patient_id}
        )
    ).first()
    if local_patient:
        local_patient_id = local_patient.id
    else:
        record = await get_cliniko().get_patient(patient_id)
        name = f"{record.get('first_name', '')} {record.get('last_name', '')}".strip() or "Unknown"
        phones = record.get("patient_phone_numbers") or []
        phone = (phones[0].get("number") if phones else "") or ""
        local_patient_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO patients (id, full_name, phone_e164, cliniko_patient_id, notes) "
                "VALUES (:id, :n, :p, :cp, 'mirrored from Cliniko (staff-created)')"
            ),
            {"id": local_patient_id, "n": name, "p": phone, "cp": patient_id},
        )

    starts = _parse_utc(remote["starts_at"])
    ends = _parse_utc(remote["ends_at"]) if remote.get("ends_at") else starts + timedelta(minutes=45)
    await session.execute(
        text(
            "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
            "appointment_type_id, during, status, fee_inr, cliniko_appointment_id, "
            "cliniko_sync_status, source_system) "
            "VALUES (:id, :pid, :prid, :bid, :tid, tstzrange(:s, :e, '[)'), 'confirmed', "
            "400, :cid, 'synced', 'cliniko')"
        ),
        {
            "id": uuid.uuid4(), "pid": local_patient_id, "prid": refs.practitioner_id,
            "bid": refs.branch_id, "tid": refs.type_id, "s": starts, "e": ends,
            "cid": str(remote["id"]),
        },
    )
    return True


async def _ticket(session, reason: str) -> None:
    await session.execute(
        text(
            "INSERT INTO followup_tickets (id, phone_e164, patient_name, reason, urgency) "
            "VALUES (gen_random_uuid(), 'reconcile', '', :reason, 'normal')"
        ),
        {"reason": reason[:500]},
    )


async def run_once() -> dict:
    """One reconcile cycle. Returns a summary dict (also alerted when drift
    was found or healing failed)."""
    now = timeutils.now_utc()
    remote = await get_cliniko().list_appointments(
        (now - timedelta(days=WINDOW_BACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (now + timedelta(days=WINDOW_AHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    summary = {"mirrored": 0, "moved": 0, "tickets": 0}
    async with SessionLocal() as session:
        local = await _load_local(session)
        drift = diff_calendars(local, remote)

        for r in drift.cliniko_only:
            try:
                if await _mirror_cliniko_appointment(session, r):
                    summary["mirrored"] += 1
                else:
                    await _ticket(session, f"Cliniko appointment {r['id']} has unmapped references — review manually")
                    summary["tickets"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the sweep
                log.warning("mirror of %s failed: %s", r.get("id"), exc)
                await _ticket(session, f"Could not mirror Cliniko appointment {r['id']}: {exc}")
                summary["tickets"] += 1

        for l, r in drift.time_mismatch:
            try:
                starts = _parse_utc(r["starts_at"])
                ends = _parse_utc(r["ends_at"]) if r.get("ends_at") else starts + timedelta(minutes=45)
                await session.execute(
                    text(
                        "UPDATE appointments SET during = tstzrange(:s, :e, '[)'), "
                        "externally_modified = true WHERE id = :id"
                    ),
                    {"s": starts, "e": ends, "id": l["id"]},
                )
                summary["moved"] += 1
            except Exception as exc:  # noqa: BLE001 — likely a local overlap; humans decide
                log.warning("time heal of %s failed: %s", l["id"], exc)
                await _ticket(session, f"Appointment {l['id']} moved in Cliniko but local update failed: {exc}")
                summary["tickets"] += 1

        for l in drift.missing_in_cliniko:
            await _ticket(
                session,
                f"Appointment {l['id']} is confirmed locally but cancelled/absent in Cliniko "
                "(staff change?) — confirm with the patient before touching it",
            )
            summary["tickets"] += 1

        await session.commit()

    if any(summary.values()):
        alerts.notify_bg(f"Reconcile: {summary['mirrored']} mirrored, {summary['moved']} moved, {summary['tickets']} tickets")
    return summary
