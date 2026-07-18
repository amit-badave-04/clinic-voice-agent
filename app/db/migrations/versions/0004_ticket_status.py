"""Escalation ticket lifecycle.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19

Warm transfer turns followup_tickets into a small state machine driven by
Retell's transfer webhooks: open → transfer_started → transfer_bridged →
transfer_completed, with transfer_failed → (agent falls back to a callback
ticket). Plain callback tickets stay 'open' until staff closes them.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE followup_tickets ADD COLUMN status text NOT NULL DEFAULT 'open'")


def downgrade() -> None:
    op.execute("ALTER TABLE followup_tickets DROP COLUMN IF EXISTS status")
