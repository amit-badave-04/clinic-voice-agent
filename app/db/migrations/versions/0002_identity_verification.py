"""Caller identity verification — OTP challenges, scoped verified sessions,
and an append-only auth audit ledger.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18

Caller ID is a routing hint, not proof of identity (PSTN caller ID is
spoofable; the web demo's caller field is free-form). Disclosure or
modification of existing appointments requires an OTP sent to the number on
file, confirmed in-call; success mints a short-lived session scoped to this
call. Hand-authored DDL, matching 0001's style.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


DDL = """
CREATE TABLE verification_challenges (
    id uuid PRIMARY KEY,
    call_id text NOT NULL,
    phone_e164 text NOT NULL,
    channel text NOT NULL DEFAULT 'sms',
    code_hash text NOT NULL DEFAULT '',
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_verification_challenges_call ON verification_challenges (call_id, created_at);

CREATE TABLE verified_sessions (
    id uuid PRIMARY KEY,
    call_id text NOT NULL,
    phone_e164 text NOT NULL,
    method text NOT NULL DEFAULT 'sms_otp',
    scope text NOT NULL DEFAULT 'existing_appointments',
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_verified_sessions_call ON verified_sessions (call_id, expires_at);

CREATE TABLE auth_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id text,
    phone_e164 text,
    event text NOT NULL,
    detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_auth_events_call ON auth_events (call_id, created_at)
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_events")
    op.execute("DROP TABLE IF EXISTS verified_sessions")
    op.execute("DROP TABLE IF EXISTS verification_challenges")
