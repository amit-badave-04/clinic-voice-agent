"""Pure-logic tests for the drift bucket classifier (no network, no DB)."""
from datetime import datetime, timezone

from app.services.reconcile import diff_calendars


def _utc(hour: int) -> datetime:
    return datetime(2026, 7, 20, hour, 0, tzinfo=timezone.utc)


LOCAL_SYNCED = {"id": "L1", "cliniko_appointment_id": "900", "status": "confirmed", "starts_at": _utc(5)}
REMOTE_MATCH = {"id": 900, "starts_at": "2026-07-20T05:00:00Z", "ends_at": "2026-07-20T05:45:00Z"}


def test_in_sync_produces_no_drift():
    drift = diff_calendars([LOCAL_SYNCED], [REMOTE_MATCH])
    assert not drift.cliniko_only and not drift.time_mismatch and not drift.missing_in_cliniko


def test_staff_created_appointment_is_cliniko_only():
    staff = {"id": 901, "starts_at": "2026-07-20T09:00:00Z"}
    drift = diff_calendars([LOCAL_SYNCED], [REMOTE_MATCH, staff])
    assert [r["id"] for r in drift.cliniko_only] == [901]


def test_staff_moved_appointment_is_time_mismatch():
    moved = dict(REMOTE_MATCH, starts_at="2026-07-20T07:00:00Z")
    drift = diff_calendars([LOCAL_SYNCED], [moved])
    assert len(drift.time_mismatch) == 1
    assert drift.time_mismatch[0][0]["id"] == "L1"


def test_cancelled_in_cliniko_is_missing():
    cancelled = dict(REMOTE_MATCH, cancelled_at="2026-07-19T00:00:00Z")
    drift = diff_calendars([LOCAL_SYNCED], [cancelled])
    assert [l["id"] for l in drift.missing_in_cliniko] == ["L1"]
    # A cancelled remote row must not be treated as staff-created either.
    assert not drift.cliniko_only


def test_unsynced_local_rows_are_ignored():
    unsynced = {"id": "L2", "cliniko_appointment_id": None, "status": "confirmed", "starts_at": _utc(6)}
    drift = diff_calendars([unsynced], [])
    assert not drift.missing_in_cliniko  # outbox owns pending-sync rows


def test_locally_cancelled_rows_are_ignored():
    cancelled_local = dict(LOCAL_SYNCED, status="cancelled")
    drift = diff_calendars([cancelled_local], [])
    assert not drift.missing_in_cliniko
