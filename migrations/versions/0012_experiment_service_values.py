"""Allow heavy-service experiment kinds and skipped research results.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("experiments_kind_check", "experiments", type_="check")
    op.create_check_constraint(
        "experiments_kind_check",
        "experiments",
        "kind IN ('BACKTEST', 'WALK_FORWARD', 'SENSITIVITY', 'PAPER_COMPARISON', 'RESEARCH_BATCH')",
    )
    op.drop_constraint("experiments_status_check", "experiments", type_="check")
    op.create_check_constraint(
        "experiments_status_check",
        "experiments",
        "status IN ('COMPLETED', 'REJECTED', 'INCONCLUSIVE', 'SKIPPED')",
    )


def downgrade() -> None:
    # Hold the same lock ALTER TABLE needs so inserts cannot race the history check.
    op.execute("LOCK TABLE experiments IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM experiments
                WHERE kind IN ('PAPER_COMPARISON', 'RESEARCH_BATCH') OR status = 'SKIPPED'
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade 0012: experiment history uses newer kinds or statuses';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("experiments_kind_check", "experiments", type_="check")
    op.create_check_constraint(
        "experiments_kind_check",
        "experiments",
        "kind IN ('BACKTEST', 'WALK_FORWARD', 'SENSITIVITY')",
    )
    op.drop_constraint("experiments_status_check", "experiments", type_="check")
    op.create_check_constraint(
        "experiments_status_check",
        "experiments",
        "status IN ('COMPLETED', 'REJECTED', 'INCONCLUSIVE')",
    )
