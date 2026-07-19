"""Adversarial security regressions (see security/REVIEW.md + security/REMEDIATION.md).

Style matches the existing suite: async, live/CI Postgres, dev-prefix phone
numbers (fictional, unreachable) with an autouse cleanup. Every assertion is on
backend state or the tool response, never on the agent's spoken words.

These now assert the REMEDIATED (secure) behavior — the findings they cover have
been fixed. Run in CI (throwaway Postgres) or locally when the live number is
NOT being tested; the migration set must be at head (alembic upgrade head) so the
DOB column and the per-patient overlap constraint from 0005 exist.
"""
import uuid
from datetime import date, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal
from app.services import timeutils, verification

settings = get_settings()

DEV = settings.otp_dev_prefix  # "+919000000" — fictional/unreachable range
PHONE_X = f"{DEV}771"          # "caller" own number
PHONE_Y = f"{DEV}772"          # "victim" number the caller does not control
PHONE_FAM = f"{DEV}773"        # shared "family line"
PHONE_BOOK = f"{DEV}774"       # existing patient for the book-leak test
PHONE_BUDGET = f"{DEV}775"     # rate-limit budget test
REAL_CALLER = "+14155550123"   # a plausible non-dev caller ID
REAL_CALLER2 = "+14155550124"
ALL_PHONES = [PHONE_X, PHONE_Y, PHONE_FAM, PHONE_BOOK, PHONE_BUDGET, REAL_CALLER, REAL_CALLER2]
CALL_IDS = [
    "sec-xnum", "sec-devchan", "sec-l2", "sec-transfer-dedupe", "sec-budget",
    "sec-sms", "sec-sms2", "sec-scope", "sec-other",
]


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with SessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM appointments WHERE patient_id IN "
                "(SELECT id FROM patients WHERE phone_e164 = ANY(:p))"
            ),
            {"p": ALL_PHONES},
        )
        await session.execute(text("DELETE FROM patients WHERE phone_e164 = ANY(:p)"), {"p": ALL_PHONES})
        for table in ("verification_challenges", "verified_sessions", "auth_events"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE phone_e164 = ANY(:p)"), {"p": ALL_PHONES}
            )
        await session.execute(text("DELETE FROM auth_events WHERE call_id = ANY(:c)"), {"c": CALL_IDS})
        await session.execute(
            text("DELETE FROM verification_challenges WHERE call_id = ANY(:c)"), {"c": CALL_IDS}
        )
        await session.execute(text("DELETE FROM followup_tickets WHERE call_id = ANY(:c)"), {"c": CALL_IDS})
        await session.execute(text("DELETE FROM call_sessions WHERE call_id = ANY(:c)"), {"c": CALL_IDS})
        await session.commit()


def _client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _auth() -> dict:
    return {"X-Tool-Secret": settings.tool_shared_secret}


async def _mint_verified(call_id: str, phone: str) -> None:
    from app.db.models import VerifiedSession

    async with SessionLocal() as session:
        session.add(
            VerifiedSession(
                id=uuid.uuid4(),
                call_id=call_id,
                phone_e164=phone,
                method="dev_otp",
                expires_at=timeutils.now_utc() + timedelta(minutes=30),
            )
        )
        await session.commit()


async def _seed_patient_with_appt(phone: str, full_name: str, dob: str | None = None) -> str:
    """Insert one patient (+ one future confirmed appointment) on `phone`. Returns
    appointment_id. Skips if the reference data is absent."""
    async with SessionLocal() as session:
        # Unique output labels + a non-colliding table alias (atype, not `t`):
        # a `t.id AS t` alias collides with the table alias and makes the Row
        # column resolve to the table's composite instead of the scalar id.
        refs = (
            await session.execute(
                text(
                    "SELECT pr.id AS practitioner_id, b.id AS branch_id, atype.id AS type_id "
                    "FROM practitioners pr "
                    "JOIN practitioner_branches pb ON pb.practitioner_id = pr.id "
                    "JOIN branches b ON b.id = pb.branch_id "
                    "CROSS JOIN appointment_types atype LIMIT 1"
                )
            )
        ).mappings().first()
        if not refs:
            pytest.skip("reference data not seeded (run seed.ci_seed / seed.local_seed)")
        pr_id, b_id, t_id = refs["practitioner_id"], refs["branch_id"], refs["type_id"]
        pid = uuid.uuid4()
        # asyncpg binds a real datetime.date for a date column — not a string.
        dob_val = date.fromisoformat(dob) if dob else None
        await session.execute(
            text(
                "INSERT INTO patients (id, full_name, phone_e164, date_of_birth) "
                "VALUES (:id, :n, :p, :dob)"
            ),
            {"id": pid, "n": full_name, "p": phone, "dob": dob_val},
        )
        # Spread starts by name hash so two patients on one number don't collide
        # under the new per-patient overlap constraint.
        offset = 40 + (abs(hash(full_name)) % 20)
        start = (timeutils.now_local() + timedelta(days=offset)).replace(
            hour=11, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        appt_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                "appointment_type_id, during, status, fee_inr, cliniko_sync_status) VALUES "
                "(:id, :pid, :pr, :b, :t, tstzrange(:s, :e, '[)'), 'confirmed', 400, 'pending')"
            ),
            {
                "id": appt_id, "pid": pid, "pr": pr_id, "b": b_id, "t": t_id,
                "s": start, "e": start + timedelta(minutes=45),
            },
        )
        await session.commit()
    return str(appt_id)


# ── C1 — auth rejection matrix ──────────────────────────────────────────────

async def test_wrong_tool_secret_and_signature_rejected():
    body = {"name": "get_patient_record", "call": {}, "args": {}}
    async with _client() as client:
        assert (await client.post("/tools/get_patient_record", json=body)).status_code == 401
        assert (
            await client.post("/tools/get_patient_record", json=body, headers={"X-Tool-Secret": "wrong"})
        ).status_code == 401
        assert (
            await client.post(
                "/tools/get_patient_record", json=body, headers={"X-Retell-Signature": "v=1,d=bogus"}
            )
        ).status_code == 401


# ── H1/H3 boundary — verification is (call_id, phone)-scoped ────────────────

async def test_verification_is_phone_scoped():
    call = "sec-scope"
    await _mint_verified(call, PHONE_X)
    async with SessionLocal() as session:
        assert await verification.is_verified(session, call, PHONE_X) is True
        assert await verification.is_verified(session, call, PHONE_Y) is False
        assert await verification.is_verified(session, "sec-other", PHONE_X) is False


async def test_get_patient_record_blocks_cross_number_when_verified_for_own(monkeypatch):
    from app.tools import router

    monkeypatch.setattr(router.settings, "require_verification", True)
    call = "sec-xnum"
    # A victim record exists on PHONE_Y; the caller is verified only for PHONE_X.
    await _seed_patient_with_appt(PHONE_Y, "Victim Person", dob="1980-01-01")
    await _mint_verified(call, PHONE_X)
    async with _client() as client:
        # Verified for own number -> allowed (PHONE_X has no record -> new_patient).
        own = await client.post(
            "/tools/get_patient_record",
            json={"name": "get_patient_record", "call": {"call_id": call, "from_number": PHONE_X}, "args": {}},
            headers=_auth(),
        )
        assert own.json()["status"] in {"new_patient", "found"}
        # Passing a DIFFERENT number in args must NOT disclose it: the caller-ID
        # (verified PHONE_X) wins, so PHONE_Y's record is never queried. This is
        # the H3 precedence fix — the arg can no longer redirect the lookup.
        cross = await client.post(
            "/tools/get_patient_record",
            json={
                "name": "get_patient_record",
                "call": {"call_id": call, "from_number": PHONE_X},
                "args": {"patient_phone": PHONE_Y},
            },
            headers=_auth(),
        )
    payload = cross.json()
    assert "Victim Person" not in str(payload)
    assert payload["status"] == "new_patient"


# ── H1 — pre-answer context carries NO patient PII (FIXED) ─────────────────

async def test_inbound_context_hides_appointments_pre_verification():
    from app.services import sessions as sessions_svc

    await _seed_patient_with_appt(PHONE_X, "Test Caller", dob="1990-01-01")
    async with SessionLocal() as session:
        variables = await sessions_svc.build_inbound_context(session, PHONE_X)
    # Routing booleans are allowed; names/appointments/summaries are not.
    assert variables["known_patient"] == "true"
    assert variables["upcoming_appointments"] == "none"
    assert variables["patient_names"] == ""
    assert variables["last_interaction"] == "none"


# ── H3 — book_appointment does not echo an existing name unverified (FIXED) ─

async def test_book_does_not_leak_existing_name_unverified(monkeypatch):
    from app.tools import router

    monkeypatch.setattr(router.settings, "require_verification", True)
    await _seed_patient_with_appt(PHONE_BOOK, "Aaaaa Bbbbb", dob="1985-05-05")
    async with _client() as client:
        resp = await client.post(
            "/tools/book_appointment",
            json={
                "name": "book_appointment",
                # Caller ID IS the number being booked (spoofed line), unverified.
                "call": {"call_id": "sec-book", "from_number": PHONE_BOOK},
                "args": {"slot_id": "irrelevant", "patient_full_name": "Aaaaa Bbbbc"},
            },
            headers=_auth(),
        )
    payload = resp.json()
    # A near-match must still bounce for confirmation, but WITHOUT revealing the name.
    assert payload.get("status") == "need_name_confirmation"
    assert payload.get("suggested_match") is None
    assert "Aaaaa Bbbbb" not in str(payload)


# ── Dev-OTP channel isolation ──────────────────────────────────────────────

async def test_dev_otp_unreachable_for_real_caller_id(monkeypatch):
    # Force the SMS path to the "unconfigured" branch so this test NEVER sends a
    # real SMS, regardless of live Twilio creds in the environment. What it
    # proves (a real caller ID cannot arm the fixed-code dev channel) depends
    # only on the phone-prefix check, not on Twilio being configured.
    monkeypatch.setattr(verification.settings, "twilio_verify_service_sid", "")
    monkeypatch.setattr(verification.settings, "twilio_account_sid", "")
    async with _client() as client:
        resp = await client.post(
            "/tools/send_verification_code",
            json={
                "name": "send_verification_code",
                "call": {"call_id": "sec-devchan", "from_number": REAL_CALLER},
                "args": {"patient_phone": f"{DEV}001"},
            },
            headers=_auth(),
        )
        assert resp.json()["status"] == "sms_unavailable"
    async with SessionLocal() as session:
        dev = (
            await session.execute(
                text(
                    "SELECT count(*) AS n FROM verification_challenges "
                    "WHERE call_id = 'sec-devchan' AND channel = 'dev'"
                )
            )
        ).first()
        assert dev.n == 0


# ── M3 — verification default is fail-safe (FIXED) ─────────────────────────

def test_verification_default_is_on():
    from app.config import Settings

    assert Settings.model_fields["require_verification"].default is True


# ── M2 — Turnstile fails closed in production (FIXED) ──────────────────────

async def test_turnstile_fails_closed_in_production(monkeypatch):
    from app.services import guard

    monkeypatch.setattr(guard.settings, "turnstile_secret_key", "")
    monkeypatch.setattr(guard.settings, "environment", "production")
    assert await guard.verify_turnstile(None, "1.2.3.4") is False
    # ...but an explicit non-production environment keeps the dev convenience.
    monkeypatch.setattr(guard.settings, "environment", "development")
    assert await guard.verify_turnstile(None, "1.2.3.4") is True


# ── M4 — shared-number access needs a per-patient factor (FIXED) ───────────

async def test_shared_number_requires_patient_factor():
    from app.services import booking

    await _seed_patient_with_appt(PHONE_FAM, "Alpha Person", dob="1970-02-02")
    appt_b = await _seed_patient_with_appt(PHONE_FAM, "Beta Person", dob="1975-03-03")
    async with SessionLocal() as session:
        # Name alone (no DOB) cannot select a co-tenant on a shared number.
        target, problem = await booking._resolve_target_appointment(
            session, PHONE_FAM, patient_name="Alpha Person", appointment_id=appt_b
        )
        assert target is None
        assert problem and problem["status"] == "need_patient_identification"
        # Even with Alpha's correct name+DOB, Beta's appointment_id is refused.
        target2, problem2 = await booking._resolve_target_appointment(
            session, PHONE_FAM, patient_name="Alpha Person", appointment_id=appt_b,
            patient_dob="1970-02-02",
        )
        assert target2 is None and problem2 is not None


# ── M6 — repeated transfer requests dedupe (FIXED) ─────────────────────────

async def test_repeated_transfer_requests_dedupe(monkeypatch):
    from app.services import transfer

    monkeypatch.setattr(transfer.settings, "staff_transfer_target", "+919999999999")
    monkeypatch.setattr(transfer, "in_transfer_window", lambda now_local=None: True)
    monkeypatch.setattr(transfer.alerts, "notify_bg", lambda *a, **k: None)
    call = "sec-transfer-dedupe"
    async with SessionLocal() as session:
        await transfer.build_plan(session, call, PHONE_X, True, "reason one")
        await transfer.build_plan(session, call, PHONE_X, True, "reason two")
        await session.commit()
    async with SessionLocal() as session:
        n = (
            await session.execute(
                text("SELECT count(*) AS n FROM followup_tickets WHERE call_id = :c"), {"c": call}
            )
        ).first().n
    assert n == 1


# ── L2 — missing conversation id fails closed (FIXED) ──────────────────────

async def test_missing_call_id_rejected():
    async with _client() as client:
        resp = await client.post(
            "/tools/get_patient_record",
            json={"name": "get_patient_record", "call": {}, "args": {"patient_phone": f"{DEV}001"}},
            headers=_auth(),
        )
    assert resp.status_code == 400


# ── H2 — per-conversation tool-call budget (NEW) ───────────────────────────

async def test_tool_call_budget_enforced(monkeypatch):
    from app.tools import router

    monkeypatch.setattr(router.settings, "max_tool_calls_per_call", 3)
    call = "sec-budget"
    async with _client() as client:
        statuses = []
        for _ in range(4):
            r = await client.post(
                "/tools/get_patient_record",
                json={"name": "get_patient_record", "call": {"call_id": call}, "args": {"patient_phone": PHONE_BUDGET}},
                headers=_auth(),
            )
            statuses.append(r.json().get("status"))
    assert statuses[-1] == "rate_limited"
    assert "rate_limited" not in statuses[:3]


# ── M1 — global daily SMS ceiling (NEW) ────────────────────────────────────

async def test_global_sms_ceiling_blocks_further_sends(monkeypatch):
    from app.db.models import VerificationChallenge

    monkeypatch.setattr(verification.settings, "max_sms_per_day", 1)
    async with SessionLocal() as session:
        session.add(
            VerificationChallenge(
                id=uuid.uuid4(),
                call_id="sec-sms",
                phone_e164=REAL_CALLER,
                channel="sms",
                expires_at=timeutils.now_utc() + timedelta(minutes=10),
            )
        )
        await session.commit()
    async with SessionLocal() as session:
        result = await verification.start_challenge(session, "sec-sms2", REAL_CALLER2)
        await session.commit()
    assert result["status"] == "sms_unavailable"
    async with SessionLocal() as session:
        ev = (
            await session.execute(
                text("SELECT detail FROM auth_events WHERE call_id = 'sec-sms2' ORDER BY id DESC LIMIT 1")
            )
        ).first()
    assert ev is not None and "ceiling" in (ev.detail or "")


# ── L5 — per-patient overlap is a DB constraint (NEW) ──────────────────────

async def test_same_patient_overlap_blocked_by_constraint():
    """Two overlapping confirmed appointments for one patient with DIFFERENT
    practitioners must be rejected by no_patient_overlap (0005) — the
    practitioner constraint cannot fire, so this isolates the new guard.
    Needs >= 2 practitioners linked to a branch; skips otherwise (e.g. ci_seed)."""
    from sqlalchemy.exc import IntegrityError

    async with SessionLocal() as session:
        pracs = (
            await session.execute(
                text(
                    "SELECT DISTINCT pr.id AS pr, pb.branch_id AS b FROM practitioners pr "
                    "JOIN practitioner_branches pb ON pb.practitioner_id = pr.id LIMIT 2"
                )
            )
        ).all()
        appt_type = (await session.execute(text("SELECT id FROM appointment_types LIMIT 1"))).first()
        if len(pracs) < 2 or not appt_type:
            pytest.skip("needs >=2 practitioners + an appointment type (run seed.local_seed)")
        pid = uuid.uuid4()
        await session.execute(
            text("INSERT INTO patients (id, full_name, phone_e164) VALUES (:id, 'Overlap Test', :p)"),
            {"id": pid, "p": PHONE_X},
        )
        start = (timeutils.now_local() + timedelta(days=70)).replace(
            hour=9, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

        async def _insert(pr, branch):
            await session.execute(
                text(
                    "INSERT INTO appointments (id, patient_id, practitioner_id, branch_id, "
                    "appointment_type_id, during, status, fee_inr) VALUES "
                    "(:id, :pid, :pr, :b, :t, tstzrange(:s, :e, '[)'), 'confirmed', 400)"
                ),
                {"id": uuid.uuid4(), "pid": pid, "pr": pr, "b": branch, "t": appt_type.id,
                 "s": start, "e": start + timedelta(minutes=45)},
            )

        await _insert(pracs[0].pr, pracs[0].b)
        await session.flush()
        with pytest.raises(IntegrityError):
            await _insert(pracs[1].pr, pracs[1].b)  # different practitioner, same patient, overlapping
            await session.flush()
    # session rolled back on error at context exit; cleanup fixture removes rows.
