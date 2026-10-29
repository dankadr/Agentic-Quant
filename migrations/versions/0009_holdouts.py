"""Holdout consumption ledger: every inspected out-of-sample period, recorded once.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE holdout_consumptions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        period_start date NOT NULL,
        period_end date NOT NULL,
        purpose text NOT NULL,
        consumed_by text NOT NULL,
        consumed_at timestamptz NOT NULL,
        CHECK (period_end >= period_start)
    )
    """,
    """
    CREATE TRIGGER holdout_consumptions_immutable BEFORE UPDATE OR DELETE ON holdout_consumptions
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS holdout_consumptions")
