"""Operations: durable incidents, retryable alert deliveries, and dashboard sessions.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE incidents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_key text NOT NULL,
        severity text NOT NULL CHECK (severity IN ('CRITICAL', 'WARNING')),
        state text NOT NULL CHECK (state IN ('OPEN', 'RESOLVED')),
        title text NOT NULL,
        detail text NOT NULL,
        opened_at timestamptz NOT NULL,
        resolved_at timestamptz,
        acknowledged_by text,
        acknowledged_at timestamptz,
        CHECK ((state = 'RESOLVED') = (resolved_at IS NOT NULL))
    )
    """,
    # One open incident per key at a time: repeated detection is deduplicated, not re-alerted.
    "CREATE UNIQUE INDEX incidents_one_open_per_key ON incidents (incident_key) WHERE state = 'OPEN'",
    """
    CREATE TABLE incident_deliveries (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id uuid NOT NULL REFERENCES incidents(id),
        event text NOT NULL CHECK (event IN ('OPENED', 'RECOVERED')),
        channel text NOT NULL,
        state text NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED')),
        attempts integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL,
        last_error text,
        created_at timestamptz NOT NULL,
        sent_at timestamptz,
        UNIQUE (incident_id, event, channel)
    )
    """,
    "CREATE INDEX incident_deliveries_due_idx ON incident_deliveries (state, next_attempt_at)",
    """
    CREATE TABLE ui_sessions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        token_hash text NOT NULL UNIQUE,
        principal_id uuid NOT NULL REFERENCES principals(id),
        csrf_token text NOT NULL,
        created_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz
    )
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table in ("ui_sessions", "incident_deliveries", "incidents"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
