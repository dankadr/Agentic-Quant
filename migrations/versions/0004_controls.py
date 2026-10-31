"""Controls: EXIT_ONLY and FLATTENING trading states, and durable operational commands.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_STATEMENTS = [
    # The original constraint was declared inline and so has Postgres' default name.
    "ALTER TABLE system_state DROP CONSTRAINT IF EXISTS system_state_trading_state_check",
    """
    ALTER TABLE system_state ADD CONSTRAINT system_state_trading_state_ck CHECK (trading_state IN (
        'PAUSED', 'PAPER_RUNNING', 'EXIT_ONLY', 'FLATTENING'))
    """,
    """
    CREATE TABLE operational_commands (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind text NOT NULL CHECK (kind IN ('STOP_ENTRIES', 'CANCEL_OPEN_ORDERS', 'FLATTEN')),
        status text NOT NULL CHECK (status IN ('REQUESTED', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        requested_by text NOT NULL,
        request_id text,
        reason text NOT NULL,
        requested_version integer NOT NULL,
        requested_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        completed_at timestamptz,
        progress_json jsonb NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX operational_commands_open_idx ON operational_commands (status)"
    " WHERE status IN ('REQUESTED', 'IN_PROGRESS')",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Additive migration: downgrading would fail on rows in the new states, so refuse explicitly.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM system_state WHERE trading_state IN"
        " ('EXIT_ONLY', 'FLATTENING')) THEN RAISE EXCEPTION 'system_state is in a 0004 state';"
        " END IF; END $$"
    )
    op.execute("DROP TABLE IF EXISTS operational_commands")
    op.execute("ALTER TABLE system_state DROP CONSTRAINT IF EXISTS system_state_trading_state_ck")
    op.execute(
        "ALTER TABLE system_state ADD CONSTRAINT system_state_trading_state_check"
        " CHECK (trading_state IN ('PAUSED', 'PAPER_RUNNING'))"
    )
