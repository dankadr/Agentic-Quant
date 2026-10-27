"""Agent config proposals: reviewed edits to agent settings, applied at a session boundary.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE agent_config_proposals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_name text NOT NULL,
        changes_json jsonb NOT NULL,
        base_json jsonb NOT NULL,
        rationale text NOT NULL,
        status text NOT NULL CHECK (status IN
            ('PENDING_REVIEW', 'APPROVED', 'REJECTED', 'APPLIED', 'SUPERSEDED')),
        source text NOT NULL CHECK (source IN ('OPERATOR', 'SUGGESTION')),
        evidence_json jsonb,
        proposed_by text NOT NULL,
        created_at timestamptz NOT NULL,
        reviewed_by text,
        reviewed_at timestamptz,
        applied_at timestamptz
    )
    """,
    "CREATE INDEX ix_agent_config_proposals_status ON agent_config_proposals (status, created_at)",
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO trading_app;
            REVOKE ALL ON alembic_version FROM trading_app;
        END IF;
    END
    $$
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_config_proposals")
