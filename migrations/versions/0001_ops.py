"""Operational schema: principals, idempotency, jobs, leases, outbox, audit, system state.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
    END;
    $$
    """,
    """
    CREATE TABLE principals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name text NOT NULL UNIQUE,
        role text NOT NULL CHECK (role IN ('viewer', 'operator')),
        api_key_hash text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL,
        disabled_at timestamptz
    )
    """,
    """
    CREATE TABLE idempotency_keys (
        principal_id uuid NOT NULL REFERENCES principals(id),
        route text NOT NULL,
        key text NOT NULL,
        request_hash text NOT NULL,
        status_code integer NOT NULL,
        response_json jsonb NOT NULL,
        created_at timestamptz NOT NULL,
        PRIMARY KEY (principal_id, route, key)
    )
    """,
    """
    CREATE TABLE jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind text NOT NULL,
        payload_json jsonb NOT NULL,
        state text NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'DEAD')),
        scheduled_at timestamptz NOT NULL,
        lease_until timestamptz,
        locked_by text,
        attempts integer NOT NULL DEFAULT 0,
        max_attempts integer NOT NULL DEFAULT 5,
        last_error text,
        dedupe_key text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX ix_jobs_state_scheduled ON jobs (state, scheduled_at)",
    """
    CREATE TABLE leases (
        name text PRIMARY KEY,
        holder text NOT NULL,
        fencing_token bigint NOT NULL,
        acquired_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE outbox (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        event_type text NOT NULL,
        payload_json jsonb NOT NULL,
        created_at timestamptz NOT NULL,
        delivered_at timestamptz
    )
    """,
    "CREATE INDEX ix_outbox_pending ON outbox (created_at) WHERE delivered_at IS NULL",
    """
    CREATE TABLE audit_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        actor text NOT NULL,
        action text NOT NULL,
        target text NOT NULL,
        ts timestamptz NOT NULL,
        request_id text,
        before_hash text,
        after_hash text,
        result text NOT NULL,
        details_json jsonb NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX ix_audit_events_ts ON audit_events (ts DESC, id DESC)",
    """
    CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
    """
    CREATE TABLE system_state (
        id smallint PRIMARY KEY CHECK (id = 1),
        mode text NOT NULL CHECK (mode = 'PAPER_ONLY'),
        trading_state text NOT NULL CHECK (trading_state IN ('PAUSED', 'PAPER_RUNNING')),
        risk_state text NOT NULL CHECK (risk_state IN (
            'NORMAL', 'HALTED_DAILY_LOSS', 'HALTED_DRAWDOWN',
            'RECONCILIATION_REQUIRED', 'UNKNOWN')),
        risk_reason text,
        version integer NOT NULL DEFAULT 1,
        updated_at timestamptz NOT NULL,
        last_reconciled_at timestamptz
    )
    """,
    """
    INSERT INTO system_state (id, mode, trading_state, risk_state, version, updated_at)
    VALUES (1, 'PAPER_ONLY', 'PAUSED', 'UNKNOWN', 1, now())
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
            GRANT USAGE ON SCHEMA public TO trading_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO trading_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_app;
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
    for table in (
        "system_state",
        "audit_events",
        "outbox",
        "leases",
        "jobs",
        "idempotency_keys",
        "principals",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
