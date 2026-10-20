Robinhood Multi-Agent Hybrid Quant + AI — MVP Specification v0.1

Date: 2026-10-18. Target: Debian server, Python 3.12, Docker Compose, $1,000 simulated capital, 5–20 potential daily entries (not a quota). Default mode: PAPER_ONLY.

Architecture and trust boundaries

ChatGPT -> custom read-mostly Management MCP -> FastAPI Control API -> PostgreSQL. Scheduler -> durable jobs -> Trading Worker -> Market Data Adapter -> Quant Signals -> Allocator -> Risk Engine -> Order State Machine -> Paper Broker. Research Agents (technical, macro, fundamental, consensus shadow) generate versioned structured research outputs; they do not possess broker credentials. The execution adapter is a replaceable interface. Robinhood live execution is disabled until capability, account, order semantics, rate limits, and legal restrictions are verified.

Only the execution worker can access the broker adapter. Risk rules are code-enforced, not prompt-enforced. API is accessible through authenticated HTTPS over Tailscale; no public unauthenticated listener. Store tokens in mounted secrets, never in logs. All times UTC internally, exchange calendars in America/New_York. Decimal for money and quantity; never binary float.

Services

● trading-api: FastAPI /v1, management MCP, OAuth/authentication gateway, RBAC, audit; no execution credentials.

● trading-worker: one active executor, deterministic strategies, risk, paper order lifecycle, reconciliation, research jobs.

● trading-scheduler: single leader; session/calendar aware; schedules market-data ingest, strategy runs, daily reconciliations.

● postgres: authoritative transactional store, durable queue with FOR UPDATE SKIP LOCKED, transactional outbox.

● prometheus: scrape API/worker metrics; no credentials or sensitive position labels.

Agents

● Technical: evaluates ORB/VWAP research and parameter candidates, but indicators/signals computed deterministically from timestamped bars.

● Macro: classifies trending/ranging/high-volatility regime; outputs bounded risk_multiplier proposal [0,1] with TTL, evidence and confidence; stale => no new entries for dependent strategies.

● Fundamental: ranks liquid large caps and flags dated corporate events; watchlist only in MVP.

● Consensus: shadow evaluation of agreement/conflict, not an execution authority.

● Evaluator: offline daily analysis and attribution; may propose but cannot deploy strategy changes.

All agent responses validated by strict Pydantic schemas, timestamped, versioned, with input snapshot IDs and TTL. Prompt injection from market news cannot change tools, rules or credentials.

API contracts

Use /v1 namespace, JSON, UTC ISO-8601 timestamps, opaque UUID identifiers, X-Request-ID correlation, pagination via cursor. POST mutations require authenticated operator role and Idempotency-Key (unique per principal+route). Errors follow RFC 7807 Problem Details. HTTP 409 for stale version/conflict, 422 for invalid configuration, 503 for unavailable state.

● GET /v1/health/live -> {status:"ok"}; GET /v1/health/ready -> {status, database, market_data, executor_lease}.

● GET /v1/system/status -> {mode:"PAPER_ONLY", trading_enabled, session, last_data_at, risk_state, last_reconciled_at}.

● GET /v1/portfolio -> {as_of, equity, cash, buying_power, realized_pnl, unrealized_pnl, exposure, currency}.

● GET /v1/positions?strategy_id=&cursor= -> positions and strategy attribution.

● GET /v1/signals?from=&to=&strategy_id=&cursor= -> validated signal records.

● GET /v1/orders?status=&cursor= -> canonical order state, broker order ID, fills.

● GET /v1/strategies -> configs, version, enabled flag, allocated notional.

● GET /v1/performance?from=&to=&strategy_id= -> equity curve, net PnL, drawdown, win rate, expectancy, fill ratio, costs.

● POST /v1/control/pause {reason, expected_version} -> {state:"PAUSED", event_id}; idempotent.

● POST /v1/control/resume-paper {reason, expected_version} -> {state:"PAPER_RUNNING", event_id}; only after ready checks.

● POST /v1/strategies/{id}/propose-config {base_version, parameters, rationale} -> {proposal_id, status:"PENDING_REVIEW"}. Separate authenticated approval endpoint applies next session, never direct free-text edits.

● GET /v1/audit?cursor= -> append-only audit events.

MCP tools (read): system_status, portfolio_summary, list_positions, list_orders, strategy_performance, risk_report, decision_trace, daily_report. Mutations: pause_paper_trading (authorized operator), propose_strategy_config (proposal only). No place_order, set_live_mode, disable_risk or raw SQL MCP tools.

Signal schema

{

  "signal_id":"uuid", "strategy_id":"orb_v1", "symbol":"SPY", "side":"BUY",

  "generated_at":"2026-10-19T14:45:00Z", "expires_at":"2026-10-19T14:46:00Z",

  "market_snapshot_id":"uuid", "strategy_version":"1.0.0",

  "target_notional_usd":"100.00", "stop_price":"575.10",

  "limit_price":"576.00", "reason_codes":["ORB_BREAK","VOLUME_CONFIRM"]

}

Signals are advisory, never broker instructions. The allocator calculates net targets; risk validates before creating an immutable order intent.

Order state machine

CREATED -> RISK_APPROVED -> SUBMITTING -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED; terminal/alternative REJECTED, CANCELED, EXPIRED, UNKNOWN. On transport timeout, mark UNKNOWN, reconcile with broker before retrying. Unique client order ID, atomic outbox, DB lock, no blind duplicate submits. Fills update position and cash transactionally. Paper fills modeled with quote availability, spread, latency, slippage, partial fills and fees; limit order is not guaranteed to fill.

Relational data model

● accounts(id, broker, mode, currency, status, created_at)

● strategies(id, name, version, enabled, capital_budget, params_json, created_at)

● instruments(id, symbol, venue, asset_type, tick_size, lot_size)

● market_bars(instrument_id, interval, ts, open, high, low, close, volume, provider, ingested_at, quality_flags) PK (instrument_id,interval,ts,provider)

● market_snapshots(id, as_of, provider, data_hash, quality_status)

● agent_runs(id, agent_name, model, prompt_version, started_at, completed_at, snapshot_id, result_json, status, cost_usd)

● signals(id, strategy_id, instrument_id, generated_at, expires_at, snapshot_id, side, score, target_notional, reason_codes, status)

● allocation_decisions(id, ts, snapshot_id, targets_json, rationale_json)

● risk_decisions(id, signal_id, ts, allowed, reasons_json, limits_version, portfolio_snapshot_id)

● orders(id, account_id, instrument_id, client_order_id UNIQUE, broker_order_id, side, type, limit_price, qty, status, created_at, updated_at)

● fills(id, order_id, execution_id UNIQUE, ts, qty, price, fees, slippage_estimate)

● cash_ledger(id, account_id, ts, amount, currency, source_type, source_id)

● positions(account_id, instrument_id, qty, average_cost, updated_at) unique (account_id,instrument_id)

● strategy_attribution(id, strategy_id, fill_id, allocated_qty, allocated_fees, pnl)

● equity_snapshots(account_id, ts, cash, equity, exposure, realized_pnl, unrealized_pnl)

● jobs(id, kind, payload_json, state, scheduled_at, lease_until, attempts, dedupe_key UNIQUE)

● outbox(id, event_type, payload_json, created_at, delivered_at)

● audit_events(id, actor, action, target, ts, request_id, before_hash, after_hash, result)

Use NUMERIC(20,8) for quantities/prices and NUMERIC(20,4) for currency amounts; FK constraints and indexes on timestamp/status/strategy. Immutable fills and ledger; corrections through reversal entries. Data retention and partitioning policies for bars/logs.

Paper risk policy ($1,000)

● Starting cash $1,000, no deposits simulated after start unless explicitly recorded.

● Gross exposure <= 80% equity; per-symbol <= 10% equity; per-sector <= 30%; no short, margin, leverage, options, or crypto.

● Max daily realized+unrealized loss <= 1% of session-start equity ($10 at $1,000); max peak-to-trough drawdown 5% ($50 at $1,000); trigger halt for new entries and alert, not guaranteed stop-loss.

● Max 10 new entries/day in first paper iteration (expand to 20 only after validated stability); max 3 open symbols; reserve >= 20% cash.

● Market hours only; new entries prohibited within last 15 minutes of regular session; flatten intraday strategy positions before close, subject to execution feasibility.

● Reject stale bars (e.g. > 90 sec for 1-minute strategy), unknown quotes, invalid spread, unavailable session/calendar, duplicate signals, invalid price/size and mismatched account state.

● Capital budgets are virtual and non-transferable by agents; allocator nets opposing intents and preserves attribution.

● Any reconciliation mismatch, provider outage, DB inconsistency or risk-state ambiguity => fail closed.

With $1,000, 10% per-symbol cap limits a position to ~$100; profit targets of 0.2–0.5% produce only $0.20–$0.50 gross per successful trade, so realistic spread/slippage and taxes may dominate. Fractional-share/order support must be verified before live use. A trading-frequency target is not a quota.

Backtesting and acceptance gates

● Timestamped historical data, corporate actions, splits/dividends, market calendar, point-in-time universe, realistic costs, no look-ahead or survivorship bias.

● Train/validation/test separation, rolling walk-forward and sensitivity analysis, multiple-hypothesis correction when many parameters are tested.

● Event-driven paper simulation with deterministic seeded replay and recorded market snapshots; shadow compare strategy versions.

● Minimum 4–8 weeks forward paper testing plus adequate trade count and varied regimes; no fixed period proves profitability.

● Operational gate: zero unexplained duplicate orders, reconciled cash/positions, tested kill switch, reproducible restart/recovery, latency/error dashboards, secrets review.

● Economic gate: positive out-of-sample expectancy after conservative costs and robustness across parameter perturbations. No guaranteed returns.

Implementation plan

Sprint 0 — Discovery / prerequisites

● Confirm Robinhood MCP supported tools, permissions, live order types, rate limits, fractional support, trading hours and account restrictions. Document gaps; keep live disabled.

● Select licensed real-time and historical market data source with quotes/spreads and retention rights.

● Define trading calendar, symbol universe, service SLOs and backup policy.

Sprint 1 — Foundation

● Docker Compose, Python packaging, FastAPI, Alembic migrations, PostgreSQL, health checks, structured logs, auth, Tailscale ingress.

● Build durable jobs, single executor lease, immutable audit and transactional outbox.

● Unit/integration tests with isolated DB.

Sprint 2 — Data / paper execution

● Provider adapter and 1-minute bar ingestion with gap detection, stale-data checks, corporate action handling.

● Paper broker order state machine, configurable fills, fees, reconciliation, portfolio ledger.

● Test crash recovery, partial fills, rejected orders, duplicate submissions.

Sprint 3 — Quant / risk

● ORB and VWAP strategies, signal validation, target allocator, risk engine, configurable session rules.

● Deterministic backtest engine, performance attribution, benchmark comparison.

● Test risk boundary conditions and kill-switch behavior.

Sprint 4 — Agents / ChatGPT

● Macro agent and Technical research agent with structured outputs, snapshot IDs and TTL.

● Fundamental watchlist and Consensus shadow mode.

● Read-mostly management MCP tools and authorized pause/proposal workflow.

Sprint 5 — Forward paper and observability

● Scheduled daily sessions, EOD report, alerts, Prometheus metrics, dashboard, restart/recovery drills.

● 4–8 weeks paper forward testing and documented go/no-go criteria.

Sprint 6 — Optional controlled live (not included in MVP)

● Verify broker execution suitability and regulatory/account constraints; obtain explicit approval.

● Separate live deployment credentials, environment, account and hard-coded live enablement gate.

● Begin with smallest supported notional and supervised order approvals before considering unattended execution.

Initial repository structure

robinhood-trading/

  docker-compose.yml

  pyproject.toml

  Dockerfile

  .env.example

  configs/{risk.yaml,strategies.yaml,agents.yaml}

  src/trading/{api,mcp,agents,quant,risk,execution,brokers,data,db,jobs,monitoring}/

  migrations/

  tests/{unit,integration,replay,chaos}/

  docs/{architecture,api-contracts,runbooks,security,decisions}/

Open decisions

1. Which market data provider and data entitlements?

2. Is Robinhood MCP appropriate for the intended execution frequency and available account features?

3. Should all paper trades use whole shares or model fractional shares where supported?

4. What is the initial 5–10-symbol universe?

5. What management MCP authentication method and exposure model are available for the user’s ChatGPT plan?

No trading order or account mutation is performed by this specification.

Expanded delivery requirements for the implementing agent

Scope and non-goals

Build a working, testable PAPER_ONLY application, not a slide deck, proof-of-concept notebook, or live trading bot. Initial allocation is virtual: Technical 35%, Fundamental/Events 20%, Macro/Trends 15%, Consensus 20%, and cash reserve 10%. For MVP, Fundamental and Consensus operate in shadow mode, so their budgets are reserved/not deployed unless an explicit versioned configuration change reassigns them. No high-frequency trading, options, margin, short selling, crypto, or unattended live execution. 5–20 potential daily entries is a research objective, never a trading quota.

Component interfaces

Define Protocols or ABCs for MarketDataProvider, ResearchAgent, SignalStrategy, CapitalAllocator, RiskEngine, BrokerAdapter, OrderRepository, and Clock. All strategies receive an immutable MarketSnapshot and return validated signal proposals. The allocator converts proposals into desired per-symbol target exposure, resolves opposing intents, and preserves attribution. The risk engine receives the proposed portfolio delta plus authoritative account/market snapshots and returns allow/deny with stable reason codes. The execution manager is the sole writer of order intents. PaperBroker implements the same broker adapter interface intended for a future live adapter.

Example order intent:

{

  "intent_id": "550e8400-e29b-41d4-a716-446655440000",

  "account_id": "paper-primary",

  "symbol": "SPY",

  "side": "BUY",

  "quantity": "0.15",

  "order_type": "LIMIT",

  "limit_price": "500.00",

  "time_in_force": "DAY",

  "strategy_ids": ["orb_v1"],

  "market_snapshot_id": "snapshot-001",

  "risk_decision_id": "risk-001",

  "client_order_id": "paper-orb-20261020-001"

}

Illustrative only: values are not live quotes. Quantity precision and fractional eligibility must be validated against instrument and broker capabilities.

Minimum error codes and invariants

Stable machine-readable rejection codes: MARKET_CLOSED, STALE_MARKET_DATA, INSUFFICIENT_CASH, EXPOSURE_LIMIT, DAILY_LOSS_LIMIT, DRAWDOWN_LIMIT, DUPLICATE_INTENT, INVALID_TICK_SIZE, UNSUPPORTED_ORDER, RECONCILIATION_REQUIRED, STRATEGY_DISABLED, SIGNAL_EXPIRED, RISK_STATE_UNKNOWN.

Invariants: cash and positions reconcile to fills and ledger; filled quantity never exceeds order quantity; no duplicate execution ID; one canonical order per client order ID; no strategy directly holds execution credentials; no live mode through configuration text alone; every order references a validated risk decision and market snapshot; rejected orders never affect positions; fees/slippage included in equity and attribution.

Agent output contract

Research outputs include agent_name, run_id, model_id, prompt_version, input_snapshot_id, created_at, valid_until, evidence, conclusion, confidence, and limitations. Technical agent proposes parameter revisions or qualitative notes; actual indicators and entry rules remain deterministic. Macro agent emits regime enum (TRENDING, RANGING, HIGH_VOLATILITY, UNKNOWN) and bounded risk multiplier. Fundamental agent emits watchlist candidates and timestamped catalysts. Consensus agent records agreement, conflict, correlated evidence, and counterarguments; it is shadow-only. Evaluator generates daily strategy attribution and drift reports. Every AI result is untrusted input until validated.

Market data specification

Store exchange-local session metadata alongside UTC timestamps. Ingest minute bars and, where licensed, bid/ask quotes. Capture provider, ingestion time, original event time, sequence/source identifier, and quality flags. Detect gaps, out-of-order events, stale quotes, zero/negative prices, splits, halts, and half-day schedules. Do not synthesize missing bars as tradable observations. Research must distinguish data available at signal time from information known later.

Paper broker behavior

Use configurable deterministic fill assumptions: quote crossing rules, spread, commission/regulatory fees, market-impact/slippage model, latency, volume participation ceiling, and partial fills. No guaranteed fills at candle high/low or signal price. Pending orders can expire or be canceled. Handle partial fills across multiple simulation ticks. Model cash settlement and buying-power constraints conservatively, especially if the eventual Robinhood account is cash rather than margin. End-of-day flattening is best-effort and must record unresolved positions.

Strategy parameter baselines (research hypotheses, not validated edge)

● ORB: regular-session opening range 15 minutes, one-minute bars, volume confirmation, spread and liquidity filter, one entry per symbol per direction per session; compare time-stop and volatility-scaled stop/target exits.

● VWAP reversion: compute session VWAP from eligible regular-session trades, standardized deviation using rolling volatility, require evidence of reversion and reject high-impact news/strong trend regimes; test time stops and bounded exits.

● Macro filter: classify market regime using broad index trend, realized volatility, sector breadth and event calendar; risk multiplier can only reduce the base exposure during MVP.

● Fundamental/events: ranked watchlist and timestamped earnings/news events, no order generation in MVP.

● Consensus: shadow counterfactual P&L versus base strategies, explicitly test whether consensus adds value net of missed opportunities.

Do not optimize parameters on the final test set. Record all experiments and rejected hypotheses.

Example configuration

system:

  mode: PAPER_ONLY

  timezone: America/New_York

  starting_cash_usd: '1000.00'

  require_fresh_market_data: true

risk:

  max_gross_exposure_pct: 0.80

  max_symbol_exposure_pct: 0.10

  max_sector_exposure_pct: 0.30

  max_daily_loss_pct: 0.01

  max_drawdown_pct: 0.05

  max_new_entries_per_day: 10

  max_open_symbols: 3

  min_cash_reserve_pct: 0.20

  allow_margin: false

  allow_short: false

  allow_options: false

  allow_crypto: false

strategies:

  orb_v1:

    enabled: true

    allocation_pct: 0.35

  vwap_v1:

    enabled: true

    allocation_pct: 0.00 # assign within technical sleeve after research

  fundamental_watchlist:

    enabled: true

    shadow_only: true

    allocation_pct: 0.20

  macro_research:

    enabled: true

    allocation_pct: 0.15

  consensus_shadow:

    enabled: true

    shadow_only: true

    allocation_pct: 0.20

  cash_reserve:

    allocation_pct: 0.10

Note: allocations describe planning sleeves, not necessarily executable invested exposure; global cash reserve of 20% takes precedence over a 10% strategic cash sleeve. The macro sleeve is a risk budget, not automatically a separately invested position. Resolve allocation semantics explicitly before writing capital allocator logic.

Acceptance tests

1. Clean docker compose up --build initializes the DB and starts healthy services without broker credentials.

2. Import historical minute bars and replay a session deterministically twice; orders/fills/P&L hashes match.

3. A signal with expired TTL or stale quotes creates no order.

4. Crossing a symbol/sector/gross exposure limit rejects the intent with a stable reason code.

5. Daily loss threshold halts new entries and persists across worker restart.

6. Simulated broker timeout enters UNKNOWN; restart reconciles without duplicate order.

7. Partial fills update cash, positions, fees and remaining order quantity correctly.

8. Two strategies with opposing target changes produce a single net order with correct attribution.

9. A malicious headline or model output cannot change risk settings, secrets or execution privileges.

10. Read-only ChatGPT MCP tools can report portfolio, order history and decision traces; cannot place trades.

11. Pause/resume-paper endpoints are authenticated, audited, idempotent and version checked.

12. A full session ends with reconciled ledger, performance report, benchmark comparison and downloadable audit trail.

Operational runbook

Startup: verify secrets and DB migrations; run health/readiness; check exchange calendar and market-data freshness; reconcile positions and cash; verify PAPER_ONLY; only then enable simulated entries. Shutdown: stop new signals, allow in-flight intents to settle or mark unknown, persist checkpoints, flush audit events, and record unresolved orders. Recovery: acquire executor lease, load authoritative state, reconcile before enabling new entries, invalidate stale signals. Alert on stale data, no heartbeat, repeated order rejection, job backlog, unreconciled positions, daily loss breach, or unexpected mode transition. Backup Postgres with tested restore procedures; redact secrets and sensitive account identifiers from logs.

Implementation order for an autonomous coding agent

1. Inspect repository/environment, record assumptions, and create an architecture decision log.

2. Scaffold Python package, Dockerfile, Compose, .env.example, lint/type/test tooling, and CI.

3. Implement schema/migrations, durable queue, idempotency, audit, and execution lease.

4. Implement mock market provider and replay fixtures before external data integration.

5. Implement paper broker, order state machine, ledger, and reconciliation with tests.

6. Implement deterministic risk engine and test all boundary conditions.

7. Implement ORB/VWAP signal strategies and capital allocation with attribution.

8. Add historical data adapter, event-driven replay, analytics, and benchmark comparisons.

9. Add research agents with schema validation and recorded evidence; no broker access.

10. Add authenticated FastAPI and read-mostly management MCP.

11. Add metrics, reports, fault-injection tests, backup/restore, and documentation.

12. Run forward paper trading and produce a go/no-go report. Do not enable live trading as part of MVP.

Deliverables expected from coding agent

A complete Git repository with runnable source, lockfile, Compose configuration, SQL migrations, typed Python models, sample safe config, automated unit/integration/replay tests, OpenAPI schema, MCP tool manifest, architecture diagram, operator runbook, security notes, and a README containing exact setup, test, replay, and paper-run commands. Report any unsupported Robinhood API/MCP features instead of inventing them. Do not request real brokerage credentials until a separately authorized live-integration phase.