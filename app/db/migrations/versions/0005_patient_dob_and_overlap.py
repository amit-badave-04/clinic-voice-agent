"""Per-patient DOB factor + per-patient overlap guard.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-19

Two security hardening changes (see security/REVIEW.md M4, L5):
  - patients.date_of_birth: a per-patient verification factor for shared "family
    line" numbers. OTP proves possession of the number; DOB proves which
    co-tenant the caller is, so a verified holder of a shared number can no
    longer read/cancel/reschedule another person's appointment.
  - no_patient_overlap: a GiST exclusion constraint making it structurally
    impossible for the SAME patient to hold two overlapping confirmed
    appointments (previously only enforced by a race-prone application check —
    two concurrent bookings could both pass it). Mirrors no_practitioner_overlap.

NOTE (operator): adding the exclusion constraint will FAIL if the table already
contains overlapping confirmed appointments for one patient. If the migration
errors on this, resolve the offending rows first (they indicate an existing
double-booking) and re-run. btree_gist already exists from 0001.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


DDL = """
ALTER TABLE patients ADD COLUMN date_of_birth date;

ALTER TABLE appointments
    ADD CONSTRAINT no_patient_overlap
    EXCLUDE USING gist (patient_id WITH =, during WITH &&)
    WHERE (status = 'confirmed')
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS no_patient_overlap")
    op.execute("ALTER TABLE patients DROP COLUMN IF EXISTS date_of_birth")
