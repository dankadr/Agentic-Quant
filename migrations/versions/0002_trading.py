"""Trading schema: market data, signals, risk, orders, fills, ledger, positions, paper venue.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_ORDER_STATUSES = (
    "'CREATED','RISK_APPROVED','SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED',"
    "'FILLED','REJECTED','CANCELED','EXPIRED','UNKNOWN'"
)
_IMMUTABLE = (
    "fills",
    "cash_ledger",
    "order_intents",
    "order_events",
    "risk_decisions",
    "paper_venue.venue_fills",
)

_STATEMENTS = [
    # --- accounts & instruments ---
    """
    CREATE TABLE accounts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name text NOT NULL UNIQUE,
        broker text NOT NULL,
        mode text NOT NULL CHECK (mode = 'PAPER'),
        currency text NOT NULL CHECK (currency = 'USD'),
        status text NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
        peak_equity numeric(20,4) NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE instruments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        symbol text NOT NULL,
        venue text NOT NULL,
        asset_type text NOT NULL CHECK (asset_type IN ('EQUITY', 'ETF')),
        tick_size numeric(20,8) NOT NULL CHECK (tick_size > 0),
        lot_size numeric(20,8) NOT NULL CHECK (lot_size > 0),
        fractional_eligible boolean NOT NULL,
        sector text NOT NULL,
        active boolean NOT NULL DEFAULT true,
        UNIQUE (symbol, venue)
    )
    """,
    # --- market data ---
    """
    CREATE TABLE market_snapshots (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        as_of timestamptz NOT NULL,
        provider text NOT NULL,
        data_hash text NOT NULL,
        quality_status text NOT NULL CHECK (quality_status IN ('OK', 'DEGRADED', 'STALE')),
        content_json jsonb NOT NULL
    )
    """,
    "CREATE INDEX ix_market_snapshots_as_of ON market_snapshots (as_of)",
    """
    CREATE TABLE market_bars (
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        interval text NOT NULL CHECK (interval IN ('1m')),
        ts timestamptz NOT NULL,
        open numeric(20,8) NOT NULL,
        high numeric(20,8) NOT NULL,
        low numeric(20,8) NOT NULL,
        close numeric(20,8) NOT NULL,
        volume numeric(20,8) NOT NULL CHECK (volume >= 0),
        provider text NOT NULL,
        ingested_at timestamptz NOT NULL,
        session_date date,
        source_seq text,
        quality_flags text[] NOT NULL DEFAULT '{}',
        PRIMARY KEY (instrument_id, interval, ts, provider)
    )
    """,
    "CREATE INDEX ix_market_bars_ts ON market_bars (ts)",
    """
    CREATE TABLE market_quotes (
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        ts timestamptz NOT NULL,
        bid numeric(20,8) NOT NULL,
        ask numeric(20,8) NOT NULL,
        bid_size numeric(20,8),
        ask_size numeric(20,8),
        provider text NOT NULL,
        ingested_at timestamptz NOT NULL,
        quality_flags text[] NOT NULL DEFAULT '{}',
        PRIMARY KEY (instrument_id, ts, provider)
    )
    """,
    """
    CREATE TABLE corporate_actions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        action_type text NOT NULL CHECK (action_type IN ('SPLIT', 'CASH_DIVIDEND')),
        ex_date date NOT NULL,
        ratio_from numeric(20,8),
        ratio_to numeric(20,8),
        cash_amount numeric(20,8),
        provider text NOT NULL,
        announced_at timestamptz NOT NULL,
        ingested_at timestamptz NOT NULL,
        UNIQUE (instrument_id, action_type, ex_date, provider),
        CHECK (action_type <> 'SPLIT' OR (ratio_from > 0 AND ratio_to > 0 AND ratio_from IS NOT NULL
            AND ratio_to IS NOT NULL)),
        CHECK (action_type <> 'CASH_DIVIDEND' OR (cash_amount IS NOT NULL AND cash_amount >= 0))
    )
    """,
    # --- strategies, signals, research ---
    """
    CREATE TABLE strategies (
        id text PRIMARY KEY,
        name text NOT NULL,
        version text NOT NULL,
        enabled boolean NOT NULL,
        capital_budget numeric(20,4) NOT NULL,
        params_json jsonb NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE signals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        strategy_id text NOT NULL REFERENCES strategies(id),
        strategy_version text NOT NULL,
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        generated_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        snapshot_id uuid NOT NULL REFERENCES market_snapshots(id),
        side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
        score numeric(20,8),
        target_notional numeric(20,4) NOT NULL,
        stop_price numeric(20,8),
        limit_price numeric(20,8),
        reason_codes text[] NOT NULL DEFAULT '{}',
        status text NOT NULL CHECK (status IN
            ('PROPOSED', 'ALLOCATED', 'REJECTED', 'EXPIRED', 'IGNORED'))
    )
    """,
    "CREATE INDEX ix_signals_strategy_generated ON signals (strategy_id, generated_at)",
    """
    CREATE TABLE allocation_decisions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ts timestamptz NOT NULL,
        snapshot_id uuid NOT NULL REFERENCES market_snapshots(id),
        targets_json jsonb NOT NULL,
        rationale_json jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE agent_runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        agent_name text NOT NULL,
        model text NOT NULL,
        prompt_version text NOT NULL,
        started_at timestamptz NOT NULL,
        completed_at timestamptz,
        snapshot_id uuid REFERENCES market_snapshots(id),
        result_json jsonb,
        status text NOT NULL CHECK (status IN
            ('RUNNING', 'COMPLETED', 'FAILED', 'REJECTED_INVALID')),
        cost_usd numeric(20,4)
    )
    """,
    # --- portfolio ---
    """
    CREATE TABLE equity_snapshots (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid NOT NULL REFERENCES accounts(id),
        ts timestamptz NOT NULL,
        kind text NOT NULL CHECK (kind IN ('INTRADAY', 'SESSION_START', 'SESSION_END')),
        cash numeric(20,4) NOT NULL,
        settled_cash numeric(20,4) NOT NULL,
        equity numeric(20,4) NOT NULL,
        exposure numeric(20,4) NOT NULL,
        realized_pnl numeric(20,4) NOT NULL,
        unrealized_pnl numeric(20,4) NOT NULL,
        positions_json jsonb NOT NULL
    )
    """,
    "CREATE INDEX ix_equity_snapshots_account_ts ON equity_snapshots (account_id, ts)",
    """
    CREATE TABLE trading_sessions (
        account_id uuid NOT NULL REFERENCES accounts(id),
        session_date date NOT NULL,
        start_equity numeric(20,4) NOT NULL,
        new_entries integer NOT NULL DEFAULT 0 CHECK (new_entries >= 0),
        daily_loss_halted_at timestamptz,
        PRIMARY KEY (account_id, session_date)
    )
    """,
    """
    CREATE TABLE reconciliation_runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid NOT NULL REFERENCES accounts(id),
        started_at timestamptz NOT NULL,
        completed_at timestamptz,
        status text NOT NULL CHECK (status IN ('OK', 'PENDING', 'MISMATCH', 'ERROR')),
        report_json jsonb NOT NULL
    )
    """,
    # --- risk, intents, orders ---
    """
    CREATE TABLE risk_decisions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        signal_id uuid REFERENCES signals(id),
        account_id uuid NOT NULL REFERENCES accounts(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        ts timestamptz NOT NULL,
        allowed boolean NOT NULL,
        reasons_json jsonb NOT NULL,
        limits_version text NOT NULL,
        market_snapshot_id uuid NOT NULL REFERENCES market_snapshots(id),
        portfolio_snapshot_id uuid REFERENCES equity_snapshots(id),
        dedupe_key text NOT NULL,
        proposal_json jsonb NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX ux_risk_decisions_allowed_dedupe ON risk_decisions (dedupe_key)"
    " WHERE allowed",
    "CREATE INDEX ix_risk_decisions_ts ON risk_decisions (ts)",
    """
    CREATE TABLE order_intents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid NOT NULL REFERENCES accounts(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity numeric(20,8) NOT NULL CHECK (quantity > 0),
        order_type text NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
        limit_price numeric(20,8),
        time_in_force text NOT NULL CHECK (time_in_force = 'DAY'),
        strategy_ids text[] NOT NULL,
        market_snapshot_id uuid NOT NULL REFERENCES market_snapshots(id),
        risk_decision_id uuid NOT NULL UNIQUE REFERENCES risk_decisions(id),
        client_order_id text NOT NULL UNIQUE,
        expires_at timestamptz,
        is_entry boolean NOT NULL,
        created_at timestamptz NOT NULL,
        CHECK (order_type <> 'LIMIT' OR (limit_price IS NOT NULL AND limit_price > 0))
    )
    """,
    """
    CREATE TABLE orders (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid NOT NULL REFERENCES accounts(id),
        intent_id uuid NOT NULL UNIQUE REFERENCES order_intents(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        client_order_id text NOT NULL UNIQUE,
        broker_order_id text,
        side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
        type text NOT NULL CHECK (type IN ('MARKET', 'LIMIT')),
        time_in_force text NOT NULL CHECK (time_in_force = 'DAY'),
        limit_price numeric(20,8),
        qty numeric(20,8) NOT NULL CHECK (qty > 0),
        filled_qty numeric(20,8) NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
        avg_fill_price numeric(20,8),
        status text NOT NULL CHECK (status IN (__STATUSES__)),
        status_reason text,
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CHECK (filled_qty <= qty),
        CHECK (type <> 'LIMIT' OR (limit_price IS NOT NULL AND limit_price > 0))
    )
    """.replace("__STATUSES__", _ORDER_STATUSES),
    "CREATE INDEX ix_orders_status ON orders (status)",
    "CREATE INDEX ix_orders_created_at ON orders (created_at)",
    """
    CREATE TABLE order_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        order_id uuid NOT NULL REFERENCES orders(id),
        ts timestamptz NOT NULL,
        from_status text,
        to_status text NOT NULL,
        reason text,
        payload_json jsonb NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX ix_order_events_order_ts ON order_events (order_id, ts)",
    """
    CREATE TABLE fills (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        order_id uuid NOT NULL REFERENCES orders(id),
        execution_id text NOT NULL UNIQUE,
        ts timestamptz NOT NULL,
        qty numeric(20,8) NOT NULL CHECK (qty > 0),
        price numeric(20,8) NOT NULL CHECK (price > 0),
        fees numeric(20,4) NOT NULL CHECK (fees >= 0),
        slippage_estimate numeric(20,4) NOT NULL CHECK (slippage_estimate >= 0)
    )
    """,
    "CREATE INDEX ix_fills_order ON fills (order_id)",
    "CREATE INDEX ix_fills_ts ON fills (ts)",
    """
    CREATE TABLE cash_ledger (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid NOT NULL REFERENCES accounts(id),
        ts timestamptz NOT NULL,
        amount numeric(20,4) NOT NULL,
        currency text NOT NULL CHECK (currency = 'USD'),
        source_type text NOT NULL CHECK (source_type IN
            ('INITIAL_DEPOSIT', 'TRADE', 'FEE', 'DIVIDEND', 'REVERSAL', 'ADJUSTMENT')),
        source_id text,
        settles_on date NOT NULL,
        reverses_id uuid REFERENCES cash_ledger(id)
    )
    """,
    "CREATE INDEX ix_cash_ledger_account_ts ON cash_ledger (account_id, ts)",
    """
    CREATE TABLE positions (
        account_id uuid NOT NULL REFERENCES accounts(id),
        instrument_id uuid NOT NULL REFERENCES instruments(id),
        qty numeric(20,8) NOT NULL CHECK (qty >= 0),
        average_cost numeric(20,8) NOT NULL,
        realized_pnl numeric(20,4) NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (account_id, instrument_id)
    )
    """,
    """
    CREATE TABLE strategy_attribution (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        strategy_id text NOT NULL REFERENCES strategies(id),
        fill_id uuid NOT NULL REFERENCES fills(id),
        allocated_qty numeric(20,8) NOT NULL,
        allocated_fees numeric(20,4) NOT NULL,
        pnl numeric(20,4)
    )
    """,
    # --- paper venue: the simulated external broker's own state ---
    "CREATE SCHEMA paper_venue",
    """
    CREATE TABLE paper_venue.venue_accounts (
        account_ref text PRIMARY KEY,
        starting_cash numeric(20,4) NOT NULL
    )
    """,
    """
    CREATE TABLE paper_venue.venue_orders (
        client_order_id text PRIMARY KEY,
        broker_order_id text NOT NULL UNIQUE,
        account_ref text NOT NULL REFERENCES paper_venue.venue_accounts(account_ref),
        symbol text NOT NULL,
        side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
        type text NOT NULL CHECK (type IN ('MARKET', 'LIMIT')),
        tif text NOT NULL CHECK (tif = 'DAY'),
        limit_price numeric(20,8),
        qty numeric(20,8) NOT NULL CHECK (qty > 0),
        filled_qty numeric(20,8) NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
        status text NOT NULL CHECK (status IN
            ('ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED')),
        submitted_at timestamptz NOT NULL,
        eligible_at timestamptz NOT NULL,
        fill_seq integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL,
        CHECK (filled_qty <= qty)
    )
    """,
    """
    CREATE TABLE paper_venue.venue_fills (
        execution_id text PRIMARY KEY,
        client_order_id text NOT NULL REFERENCES paper_venue.venue_orders(client_order_id),
        account_ref text NOT NULL,
        symbol text NOT NULL,
        side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
        ts timestamptz NOT NULL,
        qty numeric(20,8) NOT NULL CHECK (qty > 0),
        price numeric(20,8) NOT NULL CHECK (price > 0),
        fees numeric(20,4) NOT NULL,
        slippage_estimate numeric(20,4) NOT NULL
    )
    """,
]

_GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO trading_app;
        REVOKE ALL ON alembic_version FROM trading_app;
        GRANT USAGE ON SCHEMA paper_venue TO trading_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA paper_venue TO trading_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA paper_venue
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_app;
    END IF;
END
$$
"""


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)
    for table in _IMMUTABLE:
        name = table.split(".")[-1]
        op.execute(
            f"CREATE TRIGGER {name}_append_only BEFORE UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS paper_venue CASCADE")
    for table in (
        "strategy_attribution", "positions", "cash_ledger", "fills", "order_events", "orders",
        "order_intents", "risk_decisions", "reconciliation_runs", "trading_sessions",
        "equity_snapshots", "agent_runs", "allocation_decisions", "signals", "strategies",
        "corporate_actions", "market_quotes", "market_bars", "market_snapshots", "instruments",
        "accounts",
    ):  # fmt: skip
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
