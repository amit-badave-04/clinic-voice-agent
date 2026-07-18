"""Reconciliation metadata on appointments.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-19

source_system distinguishes agent-created rows from mirrors of staff-created
Cliniko appointments (the drift job inserts those so the exclusion constraint
sees staff bookings); externally_modified marks rows whose time was changed by
staff directly in Cliniko.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


DDL = """
ALTER TABLE appointments ADD COLUMN source_system text NOT NULL DEFAULT 'agent';

ALTER TABLE appointments ADD COLUMN externally_modified boolean NOT NULL DEFAULT false
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS externally_modified")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS source_system")
