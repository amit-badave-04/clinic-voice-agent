"""Availability search.

Answers "earliest slot across ALL practitioners and BOTH branches" correctly:
Cliniko's available_times endpoint is scoped to one (business, practitioner,
appointment_type), so we fan out over every candidate combo concurrently
(asyncio.gather), then merge, subtract local confirmed bookings (covers the
window where a booking exists locally but the Cliniko write-back hasn't landed),
apply the caller's fuzzy-time filters, and sort globally.

Slot IDs are opaque url-safe tokens encoding (practitioner, branch, type, start)
so book_appointment can re-validate and write without any server-side slot state.
"""
import asyncio
import base64
import json
import logging
import time as time_mod
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentType, Branch, Practitioner, PractitionerBranch
from app.services.cliniko import ClinikoError, get_cliniko
from app.services import timeutils

log = logging.getLogger("availability")

CACHE_TTL_SECONDS = 30
SLOT_VALID_MINUTES = 2

_cache: dict[str, tuple[float, list[str]]] = {}


@dataclass
class SlotCombo:
    practitioner: Practitioner
    branch: Branch
    appointment_type: AppointmentType


def encode_slot_id(practitioner_id: str, branch_id: str, appointment_type_id: str, start_utc_iso: str) -> str:
    raw = json.dumps([practitioner_id, branch_id, appointment_type_id, start_utc_iso])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_slot_id(slot_id: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, datetime]:
    padded = slot_id + "=" * (-len(slot_id) % 4)
    p, b, t, s = json.loads(base64.urlsafe_b64decode(padded).decode())
    start = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return uuid.UUID(p), uuid.UUID(b), uuid.UUID(t), start


async def resolve_appointment_type(session: AsyncSession, query: str | None) -> AppointmentType | None:
    rows = (await session.execute(select(AppointmentType))).scalars().all()
    if not rows:
        return None
    if not query:
        return next((r for r in rows if "initial" in r.key), rows[0])
    q = query.lower()
    for row in rows:
        if q == row.key or q in row.name.lower():
            return row
    # loose keyword match ("sports" -> Sports Rehab, "follow" -> Follow-up)
    for row in rows:
        if any(word in row.name.lower() for word in q.split()):
            return row
    return next((r for r in rows if "initial" in r.key), rows[0])


async def resolve_combos(
    session: AsyncSession,
    branch_key: str = "any",
    practitioner_preference: str | None = None,
    appointment_type_query: str | None = None,
) -> list[SlotCombo]:
    branches = (await session.execute(select(Branch))).scalars().all()
    if branch_key and branch_key != "any":
        branches = [b for b in branches if b.key == branch_key]
    practitioners = (await session.execute(select(Practitioner))).scalars().all()
    if practitioner_preference:
        pref = practitioner_preference.lower().replace("dr.", "").replace("dr ", "").strip()
        matched = [p for p in practitioners if pref in p.name.lower()]
        if matched:
            practitioners = matched
    links = (await session.execute(select(PractitionerBranch))).scalars().all()
    link_set = {(l.practitioner_id, l.branch_id) for l in links}
    appt_type = await resolve_appointment_type(session, appointment_type_query)
    if appt_type is None:
        return []
    combos = [
        SlotCombo(practitioner=p, branch=b, appointment_type=appt_type)
        for b in branches
        for p in practitioners
        # skip practitioners/branches not (yet) linked to Cliniko records
        if (p.id, b.id) in link_set and p.cliniko_practitioner_id and b.cliniko_business_id
    ]
    return combos


async def _fetch_combo_times(combo: SlotCombo, date_from: date, date_to: date) -> list[str]:
    """Cached (30s) Cliniko available_times for one combo. Returns UTC ISO strings."""
    key = f"{combo.branch.cliniko_business_id}:{combo.practitioner.cliniko_practitioner_id}:{combo.appointment_type.cliniko_appointment_type_id}:{date_from}:{date_to}"
    cached = _cache.get(key)
    if cached and time_mod.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    cliniko = get_cliniko()
    times: list[str] = []
    try:
        for window_from, window_to in timeutils.daterange_days(date_from, date_to):
            slots = await cliniko.available_times(
                combo.branch.cliniko_business_id,
                combo.practitioner.cliniko_practitioner_id,
                combo.appointment_type.cliniko_appointment_type_id,
                window_from,
                window_to,
            )
            times.extend(s["appointment_start"] for s in slots if "appointment_start" in s)
    except ClinikoError as exc:
        log.warning("available_times failed for %s: %s", key, exc)
        return []
    _cache[key] = (time_mod.monotonic(), times)
    return times


async def _local_busy_ranges(session: AsyncSession) -> list[tuple[uuid.UUID, datetime, datetime]]:
    """(practitioner_id, start, end) of locally confirmed appointments — subtracted
    from Cliniko slots to cover unsynced write-backs."""
    rows = await session.execute(
        text(
            "SELECT practitioner_id, lower(during) AS s, upper(during) AS e "
            "FROM appointments WHERE status = 'confirmed' AND upper(during) > now()"
        )
    )
    return [(r.practitioner_id, r.s, r.e) for r in rows]


async def search_slots(
    session: AsyncSession,
    branch: str = "any",
    appointment_type: str | None = None,
    practitioner_preference: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    weekday_mask: list[str] | None = None,
    part_of_day: list[str] | None = None,
    time_earliest: str | None = None,
    time_latest: str | None = None,
    earliest_available: bool = False,
    max_results: int = 3,
    bypass_cache: bool = False,
) -> dict:
    """Returns {"slots": [...], "retrieved_at": ..., "valid_until": ...}."""
    today = timeutils.today_local()
    date_from = max(date_from or today, today)  # Cliniko rejects past 'from'
    date_to = date_to or (date_from + timedelta(days=6))
    if date_to < date_from:
        date_to = date_from
    if (date_to - date_from).days > 27:
        date_to = date_from + timedelta(days=27)  # sane ceiling: 4 weeks

    if bypass_cache:
        _cache.clear()

    combos = await resolve_combos(session, branch, practitioner_preference, appointment_type)
    if not combos:
        return {"slots": [], "note": "no matching practitioner/branch/appointment type"}

    results = await asyncio.gather(*[_fetch_combo_times(c, date_from, date_to) for c in combos])
    busy = await _local_busy_ranges(session)

    earliest_t = timeutils.parse_time_hhmm(time_earliest)
    latest_t = timeutils.parse_time_hhmm(time_latest)
    now_utc = timeutils.now_utc()

    candidates = []
    for combo, times in zip(combos, results):
        duration = combo.appointment_type.duration_minutes + combo.appointment_type.buffer_minutes
        for iso in times:
            start = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
            if start <= now_utc:
                continue
            end = start + timedelta(minutes=duration)
            if any(p_id == combo.practitioner.id and start < e and end > s for p_id, s, e in busy):
                continue
            if not timeutils.slot_matches_filters(start, weekday_mask, part_of_day, earliest_t, latest_t):
                continue
            candidates.append((start, combo))

    candidates.sort(key=lambda pair: pair[0])
    if earliest_available:
        max_results = 1

    retrieved_at = timeutils.now_utc()
    slots = []
    for start, combo in candidates[:max_results]:
        slots.append(
            {
                "slot_id": encode_slot_id(
                    str(combo.practitioner.id),
                    str(combo.branch.id),
                    str(combo.appointment_type.id),
                    start.isoformat(),
                ),
                "when": timeutils.speakable_datetime(start),
                "starts_at_utc": start.isoformat(),
                "practitioner": combo.practitioner.name,
                "branch": combo.branch.name,
                "branch_key": combo.branch.key,
                "appointment_type": combo.appointment_type.name,
                "duration_minutes": combo.appointment_type.duration_minutes,
                "fee_inr": combo.appointment_type.fee_inr,
            }
        )

    return {
        "slots": slots,
        "count_considered": len(candidates),
        "retrieved_at": retrieved_at.isoformat(),
        "valid_until": (retrieved_at + timedelta(minutes=SLOT_VALID_MINUTES)).isoformat(),
        "note": "Slot data expires quickly; re-run this search if the caller changes preferences.",
    }
