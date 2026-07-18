"""Local Postgres schema — the integrity boundary of the system.

Cliniko deliberately allows double-bookings, has no webhooks and no idempotency
support, so correctness lives here:
  - appointments.during is a tstzrange with a GiST exclusion constraint
    (added in the alembic migration — SQLAlchemy models don't express it)
  - idempotency_keys makes booking tool calls replay-safe (Retell retries)
  - outbox implements the write-back-with-retry to Cliniko
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(Text, unique=True)  # "medax" | "arc"
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, default="")
    cliniko_business_id: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Kolkata")


class Practitioner(Base):
    __tablename__ = "practitioners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text)
    specialties: Mapped[list] = mapped_column(JSONB, default=list)
    cliniko_practitioner_id: Mapped[str | None] = mapped_column(Text)


class PractitionerBranch(Base):
    """Which practitioner works at which branch (schedules live in Cliniko)."""

    __tablename__ = "practitioner_branches"

    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("practitioners.id"), primary_key=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), primary_key=True)


class AppointmentType(Base):
    __tablename__ = "appointment_types"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(Text, unique=True)  # e.g. "initial_assessment"
    name: Mapped[str] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer)  # consult time told to patient
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0)  # gap enforced after
    fee_inr: Mapped[int] = mapped_column(Integer, default=400)
    cliniko_appointment_type_id: Mapped[str | None] = mapped_column(Text)


class Patient(Base):
    __tablename__ = "patients"
    # Several patients may share phone_e164 (family line) — no unique constraint.

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str] = mapped_column(Text, index=True)
    cliniko_patient_id: Mapped[str | None] = mapped_column(Text)
    preferred_branch: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("status IN ('confirmed', 'cancelled')", name="appointments_status_check"),
        Index("ix_appointments_patient", "patient_id"),
        Index("ix_appointments_status", "status"),
        # GiST exclusion constraint (no overlapping confirmed appts per practitioner)
        # is raw DDL in the alembic migration.
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"))
    practitioner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practitioners.id"))
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"))
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointment_types.id"))
    during = mapped_column(TSTZRANGE, nullable=False)  # [) half-open, includes buffer
    status: Mapped[str] = mapped_column(Text, default="confirmed")
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)
    fee_inr: Mapped[int] = mapped_column(Integer, default=400)
    cliniko_appointment_id: Mapped[str | None] = mapped_column(Text)
    cliniko_sync_status: Mapped[str] = mapped_column(Text, default="pending")  # synced|pending|failed
    source_system: Mapped[str] = mapped_column(Text, default="agent")  # agent|cliniko (staff-created mirror)
    externally_modified: Mapped[bool] = mapped_column(Boolean, default=False)  # staff moved it in Cliniko
    created_via_call_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, default="")
    response_body: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    """Transactional outbox for Cliniko write-back (defined behavior on PMS failure)."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text)  # create_appointment|update_appointment|cancel_appointment
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|in_flight|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallSession(Base):
    """Per-call state for dropped-call resume, keyed by caller phone."""

    __tablename__ = "call_sessions"
    __table_args__ = (Index("ix_call_sessions_phone", "phone_e164", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_e164: Mapped[str] = mapped_column(Text)
    call_id: Mapped[str] = mapped_column(Text, unique=True)
    stage: Mapped[str] = mapped_column(Text, default="started")  # started|in_task|completed
    collected: Mapped[dict] = mapped_column(JSONB, default=dict)  # entities captured so far
    summary: Mapped[str] = mapped_column(Text, default="")
    last_disconnect_reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PendingCallback(Base):
    """We called the patient, they didn't answer; when they ring back, carry context."""

    __tablename__ = "pending_callbacks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_e164: Mapped[str] = mapped_column(Text, index=True)
    context_summary: Mapped[str] = mapped_column(Text)
    owed: Mapped[bool] = mapped_column(Boolean, default=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupTicket(Base):
    """Human-follow-up log: caller asked for a person / raised a clinical concern."""

    __tablename__ = "followup_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_e164: Mapped[str] = mapped_column(Text)
    patient_name: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(Text, default="normal")  # normal|urgent
    call_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerificationChallenge(Base):
    """One OTP challenge: sent to the number on file, checked in-call.
    channel 'sms' = Twilio Verify manages the code (code_hash empty);
    channel 'dev' = locally generated code for eval/demo phone prefixes."""

    __tablename__ = "verification_challenges"
    __table_args__ = (Index("ix_verification_challenges_call", "call_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default="sms")  # sms|dev
    code_hash: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerifiedSession(Base):
    """Successful verification, scoped to one call and a short TTL. Appointment
    read/write tools require a live row here (see app/tools/router.py gate)."""

    __tablename__ = "verified_sessions"
    __table_args__ = (Index("ix_verified_sessions_call", "call_id", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text, default="sms_otp")
    scope: Mapped[str] = mapped_column(Text, default="existing_appointments")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthEvent(Base):
    """Append-only audit ledger of every verification-related event."""

    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_call", "call_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[str | None] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)  # challenge_sent|code_ok|code_bad|denied_unverified|...
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClinicPolicy(Base):
    __tablename__ = "clinic_policies"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class CallLog(Base):
    """Local record of every call (feeds sessions + the eval latency report)."""

    __tablename__ = "call_log"

    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    phone_e164: Mapped[str | None] = mapped_column(Text, index=True)
    direction: Mapped[str] = mapped_column(Text, default="inbound")  # inbound|outbound|web
    status: Mapped[str] = mapped_column(Text, default="registered")
    disconnection_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
