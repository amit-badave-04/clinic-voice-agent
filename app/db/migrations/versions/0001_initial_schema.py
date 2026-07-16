"""Initial schema — tables + GiST exclusion constraint (the no-double-booking guarantee).

Revision ID: 0001
Revises:
Create Date: 2026-07-16

Hand-authored: the exclusion constraint on appointments is the core integrity
mechanism (Cliniko itself permits double-bookings), so the DDL is explicit.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


DDL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE branches (
    id uuid PRIMARY KEY,
    key text UNIQUE NOT NULL,
    name text NOT NULL,
    address text NOT NULL DEFAULT '',
    cliniko_business_id text,
    timezone text NOT NULL DEFAULT 'Asia/Kolkata'
);

CREATE TABLE practitioners (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    specialties jsonb NOT NULL DEFAULT '[]'::jsonb,
    cliniko_practitioner_id text
);

CREATE TABLE practitioner_branches (
    practitioner_id uuid NOT NULL REFERENCES practitioners(id),
    branch_id uuid NOT NULL REFERENCES branches(id),
    PRIMARY KEY (practitioner_id, branch_id)
);

CREATE TABLE appointment_types (
    id uuid PRIMARY KEY,
    key text UNIQUE NOT NULL,
    name text NOT NULL,
    duration_minutes integer NOT NULL,
    buffer_minutes integer NOT NULL DEFAULT 0,
    fee_inr integer NOT NULL DEFAULT 400,
    cliniko_appointment_type_id text
);

CREATE TABLE patients (
    id uuid PRIMARY KEY,
    full_name text NOT NULL,
    phone_e164 text NOT NULL,
    cliniko_patient_id text,
    preferred_branch text,
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_patients_phone_e164 ON patients (phone_e164);

CREATE TABLE appointments (
    id uuid PRIMARY KEY,
    patient_id uuid NOT NULL REFERENCES patients(id),
    practitioner_id uuid NOT NULL REFERENCES practitioners(id),
    branch_id uuid NOT NULL REFERENCES branches(id),
    appointment_type_id uuid NOT NULL REFERENCES appointment_types(id),
    during tstzrange NOT NULL,
    status text NOT NULL DEFAULT 'confirmed'
        CONSTRAINT appointments_status_check CHECK (status IN ('confirmed', 'cancelled')),
    cancellation_reason text,
    reschedule_count integer NOT NULL DEFAULT 0,
    fee_inr integer NOT NULL DEFAULT 400,
    cliniko_appointment_id text,
    cliniko_sync_status text NOT NULL DEFAULT 'pending',
    created_via_call_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- The no-double-booking guarantee: overlapping confirmed appointments for
    -- the same practitioner are structurally impossible. [) half-open ranges
    -- let back-to-back slots coexist. Violation -> SQLSTATE 23P01.
    CONSTRAINT no_practitioner_overlap
        EXCLUDE USING gist (practitioner_id WITH =, during WITH &&)
        WHERE (status = 'confirmed')
);
CREATE INDEX ix_appointments_patient ON appointments (patient_id);
CREATE INDEX ix_appointments_status ON appointments (status);

CREATE TABLE idempotency_keys (
    key text PRIMARY KEY,
    request_hash text NOT NULL DEFAULT '',
    response_body jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
    id bigserial PRIMARY KEY,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_pending ON outbox (status, next_attempt_at);

CREATE TABLE call_sessions (
    id uuid PRIMARY KEY,
    phone_e164 text NOT NULL,
    call_id text UNIQUE NOT NULL,
    stage text NOT NULL DEFAULT 'started',
    collected jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary text NOT NULL DEFAULT '',
    last_disconnect_reason text,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_call_sessions_phone ON call_sessions (phone_e164, expires_at);

CREATE TABLE pending_callbacks (
    id uuid PRIMARY KEY,
    phone_e164 text NOT NULL,
    context_summary text NOT NULL,
    owed boolean NOT NULL DEFAULT true,
    attempts integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pending_callbacks_phone ON pending_callbacks (phone_e164);

CREATE TABLE followup_tickets (
    id uuid PRIMARY KEY,
    phone_e164 text NOT NULL,
    patient_name text NOT NULL DEFAULT '',
    reason text NOT NULL,
    urgency text NOT NULL DEFAULT 'normal',
    call_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clinic_policies (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE call_log (
    call_id text PRIMARY KEY,
    phone_e164 text,
    direction text NOT NULL DEFAULT 'inbound',
    status text NOT NULL DEFAULT 'registered',
    disconnection_reason text,
    summary text NOT NULL DEFAULT '',
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    ended_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_call_log_phone ON call_log (phone_e164);
"""

DROP = """
DROP TABLE IF EXISTS call_log, clinic_policies, followup_tickets, pending_callbacks,
    call_sessions, outbox, idempotency_keys, appointments, patients,
    appointment_types, practitioner_branches, practitioners, branches CASCADE;
"""


def upgrade() -> None:
    # asyncpg can't run multi-statement strings; no ';' occurs inside literals here,
    # so a plain split is safe.
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for statement in DROP.split(";"):
        if statement.strip():
            op.execute(statement)
