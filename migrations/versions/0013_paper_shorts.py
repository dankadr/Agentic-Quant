"""Signed positions and explicit order effects.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLES = (("order_intents", None), ("orders", None), ("venue_orders", "paper_venue"))


def upgrade() -> None:
    op.add_column(
        "venue_fills",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True)),
        schema="paper_venue",
    )
    op.drop_constraint("positions_qty_check", "positions", type_="check")
    for table, schema in _TABLES:
        op.add_column(table, sa.Column("position_effect", sa.Text(), nullable=True), schema=schema)
        op.create_check_constraint(
            f"{table}_effect_check",
            table,
            "position_effect IS NULL OR (side = 'BUY' AND position_effect IN ('OPEN_LONG','CLOSE_SHORT'))"
            " OR (side = 'SELL' AND position_effect IN ('OPEN_SHORT','CLOSE_LONG'))",
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM positions WHERE qty < 0)")):
        raise RuntimeError("cannot downgrade with outstanding short positions")
    for table, schema in _TABLES:
        name = f"{schema}.{table}" if schema else table
        if bind.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {name} WHERE position_effect IN ('OPEN_SHORT','CLOSE_SHORT'))"
            )
        ):
            raise RuntimeError("cannot downgrade with explicit short history")
    for table, schema in _TABLES:
        op.drop_column(table, "position_effect", schema=schema)
    op.drop_column("venue_fills", "seq", schema="paper_venue")
    op.create_check_constraint("positions_qty_check", "positions", "qty >= 0")
