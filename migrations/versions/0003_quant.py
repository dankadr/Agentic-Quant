"""Quant: config proposals, experiment registry, attribution uniqueness.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE strategy_config_proposals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        strategy_id text NOT NULL REFERENCES strategies(id),
        base_version text NOT NULL,
        parameters_json jsonb NOT NULL,
        rationale text NOT NULL,
        status text NOT NULL CHECK (status IN
            ('PENDING_REVIEW', 'APPROVED', 'REJECTED', 'APPLIED', 'SUPERSEDED')),
        proposed_by text NOT NULL,
        created_at timestamptz NOT NULL,
        reviewed_by text,
        reviewed_at timestamptz,
        applied_at timestamptz,
        applied_version text
    )
    """,
    "CREATE INDEX ix_config_proposals_status ON strategy_config_proposals (status, created_at)",
    """
    CREATE TABLE experiments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind text NOT NULL CHECK (kind IN ('BACKTEST', 'WALK_FORWARD', 'SENSITIVITY')),
        strategy_id text REFERENCES strategies(id),
        hypothesis text NOT NULL,
        params_json jsonb NOT NULL,
        data_range_json jsonb NOT NULL,
        metrics_json jsonb NOT NULL,
        status text NOT NULL CHECK (status IN ('COMPLETED', 'REJECTED', 'INCONCLUSIVE')),
        notes text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX ix_experiments_created ON experiments (created_at)",
    "ALTER TABLE strategy_attribution ADD CONSTRAINT uq_attribution_strategy_fill UNIQUE (strategy_id, fill_id)",
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
    op.execute(
        "ALTER TABLE strategy_attribution DROP CONSTRAINT IF EXISTS uq_attribution_strategy_fill"
    )
    op.execute("DROP TABLE IF EXISTS experiments")
    op.execute("DROP TABLE IF EXISTS strategy_config_proposals")
