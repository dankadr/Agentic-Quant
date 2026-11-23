"""Protect applied paper venue actions as append-only accounting facts."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER venue_actions_append_only BEFORE UPDATE OR DELETE ON paper_venue.venue_actions"
        " FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='trading_app') THEN
            REVOKE UPDATE, DELETE ON paper_venue.venue_actions FROM trading_app;
        END IF; END $$
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER venue_actions_append_only ON paper_venue.venue_actions")
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='trading_app') THEN
            GRANT UPDATE, DELETE ON paper_venue.venue_actions TO trading_app;
        END IF; END $$
    """)
