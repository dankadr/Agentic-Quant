"""Record paper venue corporate actions independently from the application ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("position_effect", sa.Text(), nullable=True))
    op.create_table(
        "venue_actions",
        sa.Column("account_ref", sa.Text(), primary_key=True),
        sa.Column("action_id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("last_fill_seq", sa.BigInteger(), nullable=False),
        sa.Column("ratio_from", sa.Numeric(20, 8), nullable=False),
        sa.Column("ratio_to", sa.Numeric(20, 8), nullable=False),
        sa.Column("cash_delta", sa.Numeric(20, 4), nullable=False),
        schema="paper_venue",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT FROM paper_venue.venue_actions)")):
        raise RuntimeError("cannot remove applied venue actions")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT FROM signals WHERE position_effect IN ('OPEN_SHORT','CLOSE_SHORT'))"
        )
    ):
        raise RuntimeError("cannot remove explicit short signal history")
    op.drop_table("venue_actions", schema="paper_venue")
    op.drop_column("signals", "position_effect")
