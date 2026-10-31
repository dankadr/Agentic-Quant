"""Let the application role read the schema version (read-only; it never writes it).

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # /v1/system/status reports the migration the database actually has. Reading it must not need
    # owner rights; writing it stays with the migration role alone.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
                GRANT SELECT ON alembic_version TO trading_app;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
                REVOKE SELECT ON alembic_version FROM trading_app;
            END IF;
        END $$
        """
    )
