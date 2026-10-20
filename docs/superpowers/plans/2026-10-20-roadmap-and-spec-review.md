# AI Quant (Robinhood Multi-Agent) — Spec Review & Roadmap

**Spec:** `docs/spec/mvp-spec-v0.1.md` (master specification, v0.1)
**Detailed plan for the first build phase:** `docs/superpowers/plans/2026-10-20-plan-a-foundation-paper-execution-risk.md`

This document does three things. It records where the spec conflicts with itself or leaves gaps, and how each one is resolved. It lists the decisions only the owner can make. It gives the checklist for the whole MVP, split into four plans.

---

## 1. Spec review

### 1.1 Conflicts and resolutions

Items marked **[call]** are the planner's call, made because the spec asks for a resolution or the build cannot proceed without one. The owner can override any of them.

| # | Conflict | Resolution |
|---|----------|------------|
| C1 | Strategy sleeves (35/20/15/20/10) vs. global limits (gross ≤ 80%, cash reserve ≥ 20%, symbol ≤ 10%, ≤ 3 open symbols). The spec asks for this to be resolved before the allocator is written. | **[call]** A sleeve % is the *maximum* notional that strategy may hold (`sleeve × equity`). Global limits always apply on top and always win. Shadow sleeves (fundamental, consensus) and the macro sleeve (a risk budget) never deploy capital in the MVP. Effective MVP ceiling: `min(35%, 3 symbols × 10%) = 30%` of equity. Implemented in Plan B. |
| C2 | A $100 per-symbol cap vs. whole shares. Most liquid large caps cost more than $100 per share, so a whole-share position cannot fit under the cap. | **[call, needs your confirmation; open decision 3]** By default the paper broker models fractional shares in 0.0001-share increments for instruments flagged `fractional_eligible`. It can be configured to whole shares only. Whether real fractional shares are supported stays unverified until Sprint 0. |
| C3 | "Risk validates *before* creating an immutable order intent" vs. an order state machine that starts `CREATED -> RISK_APPROVED`. | The risk decision is recorded first, and the intent and order (`CREATED`) reference it. Just before submitting, the execution manager re-checks that the decision is still valid: allowed, unused, unexpired, and the trading/risk state still permits it. Only then does it move the order to `RISK_APPROVED`. That gives two gates and no way to bypass them. |
| C4 | Boundary semantics, e.g. "loss ≤ 1%". | **[call]** A halt triggers when loss **≥** the limit (at exactly the limit, the safe side wins). Exposure is allowed when the post-trade value is **≤** the limit. |
| C5 | Halts block "new entries", but the spec doesn't say what happens to exits. | **[call]** Exits that reduce risk stay allowed during daily-loss and drawdown halts and in the last 15 minutes. **Everything**, exits included, is blocked when the risk state is `UNKNOWN` or `RECONCILIATION_REQUIRED`, because the account state can't be trusted then. |
| C6 | No rule for clearing a halt. | **[call]** The daily-loss halt clears automatically at the next session start. The drawdown halt needs an explicit, audited operator action (`trading admin clear-drawdown-halt`). |
| C7 | "Reconcile before retrying" UNKNOWN orders, with no rule for when the broker has no record. | **[call]** Orders are never resubmitted automatically. If the broker has no record of an `UNKNOWN`/`SUBMITTING` order 30 s after it was last updated, the order becomes `REJECTED` with reason `NOT_FOUND_AT_BROKER`. Any new attempt needs a fresh risk decision. |
| C8 | `CREATED` has no transition for an intent that expired before submission. | The order becomes `REJECTED` with `status_reason = SIGNAL_EXPIRED`. The state table is unchanged. |
| C9 | The spec example uses string account ID `paper-primary`, but IDs must be opaque UUIDs. | Accounts have a UUID `id` and a unique human `name` (`paper-primary`). |
| C10 | The example config carries all sections in one YAML. The repo layout lists `risk.yaml`, `strategies.yaml`, `agents.yaml`. | Use separate files: `configs/system.yaml` (system + universe), `risk.yaml`, `strategies.yaml`, `paper_broker.yaml`. Add `agents.yaml` in Plan C, when agents exist. |

### 1.2 Missing requirements, added

| Area | Addition | Why |
|------|----------|-----|
| Schema | `order_intents` table, immutable, with FKs to the risk decision and market snapshot, both NOT NULL | Spec invariant: "every order references a validated risk decision and market snapshot". The spec's `orders` table has no such columns. |
| Schema | `order_events` (append-only status history) | Decision traces (MCP `decision_trace`), audit, debugging |
| Schema | `orders`: `intent_id`, `time_in_force`, `filled_qty`, `avg_fill_price`, `status_reason`, `version`; CHECK `filled_qty <= qty` | Spec invariant: filled ≤ ordered |
| Schema | `market_quotes`, `corporate_actions` | The spec requires spread/quote checks and corporate actions but has no tables for them |
| Schema | `market_bars`: `session_date`, `source_seq`, `quality_flags text[]` | "Store exchange-local session metadata … sequence/source identifier, quality flags" |
| Schema | `market_snapshots.content_json` | Deterministic replay needs the snapshot contents, not just its hash |
| Schema | `instruments`: `sector`, `fractional_eligible`, `active`; asset_type CHECK `EQUITY`/`ETF` | The sector limit needs sector data. No options/crypto. |
| Schema | `accounts`: `name`, `peak_equity`; CHECK `mode = 'PAPER'` | Drawdown needs a high-water mark. Live mode cannot be reached through config text alone. |
| Schema | `positions.realized_pnl`; CHECK `qty >= 0` | No shorts, enforced in the database as well as in code |
| Schema | `cash_ledger`: `settles_on`, `reverses_id`, `source_type` CHECK | Conservative cash-account settlement; corrections only by reversal entries |
| Schema | `equity_snapshots`: `id`, `settled_cash`, `positions_json`, `kind` | `risk_decisions.portfolio_snapshot_id` needs something to point at |
| Schema | `trading_sessions` (start equity, entry count per session) | The daily loss limit and max entries per day need session-scoped state that survives a restart (acceptance test 5) |
| Schema | `reconciliation_runs` | `last_reconciled_at` and a mismatch history |
| Schema | `system_state` singleton (mode, trading state, risk state, `version`) | `expected_version` / HTTP 409 needs a version, and halts must survive a restart |
| Schema | `leases` with fencing tokens | "Single executor lease" and "single leader scheduler" need a table. Fencing stops a stale worker from writing. |
| Schema | `idempotency_keys`, `principals` | "Idempotency-Key unique per principal+route" and authentication |
| Schema | `paper_venue` schema (venue accounts/orders/fills) | The paper broker is treated as an *external* system with its own state. Without that, reconciliation compares the ledger with itself and proves nothing. |
| Security | Two DB roles: `trading_owner` (migrations) and `trading_app` (DML only, no TRUNCATE). Row triggers block UPDATE/DELETE on `fills`, `cash_ledger`, `audit_events`, `order_intents`, `risk_decisions`, `order_events`. | Immutability enforced in the database, not just by convention |
| Security | Sprint 1 auth = hashed bearer API keys with roles `viewer`/`operator`. OAuth for the ChatGPT MCP connector comes in Plan C. | The spec names OAuth but the MCP comes later. The API needs auth now. |
| Risk | Extra stable rejection codes: `TRADING_PAUSED`, `INVALID_QUOTE`, `SHORT_NOT_ALLOWED`, `INVALID_QUANTITY`, `CASH_RESERVE_LIMIT`, `MAX_DAILY_ENTRIES`, `MAX_OPEN_SYMBOLS` | The spec list is a "minimum". These checks have no code in it. |
| Risk | `max_spread_bps: '25'`, `max_bar_age_seconds: 90`, `entry_cutoff_minutes_before_close: 15`, `unknown_order_grace_seconds: 30` in `risk.yaml` | "Invalid spread" needs a threshold **[call: 25 bps]** |
| Risk | Initial state on first boot: `PAUSED`, risk state `UNKNOWN` until the first successful reconciliation | Fail closed |
| Execution | `Clock` protocol injected everywhere; no direct `datetime.now()` outside `SystemClock` | Deterministic replay |
| Execution | Cash account, T+1 settlement, `use_unsettled_funds: false`. Buying power = settled cash − open-buy reservations. | "Model cash settlement and buying power conservatively" |
| Ops | Readiness = DB reachable **and** executor lease held. `market_data` is reported (`ok`/`stale`/`closed`/`not_configured`) but only blocks `resume-paper`. | Acceptance test 1 needs healthy services before any data provider exists |
| Ops | Metrics served on internal ports (9100–9102) that are never published to the host | Prometheus scrapes the internal network. Nothing extra is exposed through Tailscale. |

### 1.3 Not verified; nothing assumed

- **Robinhood execution.** Unverified: whether Robinhood offers a supported API or MCP for placing *equity* orders on this account type, and if so its order types, rate limits, fractional-share support, extended hours and account restrictions. No Robinhood adapter code is written. `BrokerAdapter` is the only seam, and `PaperBroker` is its only implementation.
- **Fee rates.** The SEC/FINRA fee values in `configs/paper_broker.yaml` are placeholders, labelled as such. Check current rates before trusting cost estimates.
- **Fixture prices.** Replay fixtures are synthetic random walks with a fixed seed, labelled `SYNTHETIC`. They are not market data.

### 1.4 Decisions only you can make

| # | Decision | What it blocks | Default until you decide |
|---|----------|----------------|--------------------------|
| 1 | Market data provider and entitlements (real-time 1-min bars + quotes, history, corporate actions) | Paper sessions during live market hours. Replay works without it. | `CsvReplayProvider` only. During live hours the worker reports `market_data: not_configured` and places nothing. |
| 2 | Initial 5–10-symbol universe | Plan B strategies | `SPY, QQQ, IWM, AAPL, MSFT` in `configs/system.yaml` (a suggestion, not a recommendation) |
| 3 | Fractional vs. whole shares in paper | Sizing realism | Fractional (C2) |
| 4 | LLM provider/model for the research agents | Plan C | none |
| 5 | ChatGPT plan, and which MCP connector auth it supports | Plan C | none |
| 6 | Deployment host (Debian server + Tailscale), and backup target | Plan D | Develop locally with Docker Desktop. Compose is portable. |

---

## 2. Plan sequence

Each plan produces working, tested software on its own. Plans B–D are written once Plan A's interfaces exist, so they are based on real code rather than guesses.

| Plan | Scope | Spec sprints |
|------|-------|--------------|
| **A** (detailed, ready) | Infrastructure, DB, jobs/leases/audit/outbox, API + auth + control, Compose. Market data replay and ingestion, paper broker, order state machine, ledger, reconciliation. **Risk engine and halts (pulled forward from Sprint 3, as you asked).** Deterministic replay. | 1, 2, part of 3 |
| **B** | ORB and VWAP strategies, signal validation, allocator with netting and attribution, end-of-day flattening, backtest/walk-forward engine, performance and benchmark comparison, `/signals`, `/strategies`, `/performance`, propose/approve config workflow | rest of 3 |
| **C** | Research agents (macro, technical, fundamental watchlist, consensus shadow, evaluator) with schema-validated, TTL'd outputs. Management MCP server (read-mostly) with OAuth. Prompt-injection tests. | 4 |
| **D** | Prometheus dashboards, alerts, EOD report, backup/restore drill, chaos suite, forward-paper runbook, go/no-go report | 5 |
| (Sprint 0) | Robinhood capability verification and data-provider selection. These are research and decisions, not code. They run alongside A and must finish before B's live-hours paper runs. | 0 |
| (Sprint 6) | Live trading. **Out of MVP scope. Not planned.** | 6 |

---

## 3. Full MVP checklist

### Sprint 0 — Discovery (owner + research, no code)
- [ ] Verify Robinhood equity API/MCP existence, tools, permissions, order types, rate limits, fractional support, trading hours, account restrictions. Write findings to `docs/decisions/robinhood-capabilities.md`. Keep live disabled.
- [ ] Choose a licensed market data provider (decision 1).
- [ ] Fix the symbol universe (decision 2), service SLOs, and backup policy.

### Plan A — Foundation, paper execution, risk (Sprints 1–2 + risk)
- [ ] Repo scaffold, uv lockfile, ruff, mypy --strict, pytest, CI
- [ ] Core primitives: Decimal helpers, UTC enforcement, Clock, IDs, rejection codes, hashing
- [ ] Config loading; `PAPER_ONLY`, `allow_*: false` enforced by the type system
- [ ] JSON logs with secret redaction
- [ ] Postgres + Alembic, test DB harness, ops schema, DB roles, immutability triggers
- [ ] Durable job queue (`FOR UPDATE SKIP LOCKED`), runner
- [ ] Leases with fencing tokens (executor, scheduler)
- [ ] Immutable audit log, transactional outbox
- [ ] System state (trading/risk state, versioned)
- [ ] FastAPI: request IDs, RFC 7807, health live/ready, metrics
- [ ] API key auth, RBAC, Idempotency-Key, pause/resume-paper, system status, audit list
- [ ] Dockerfile, Compose (postgres, migrate, api, worker, scheduler, prometheus), secrets, smoke test, Tailscale runbook
- [ ] Trading schema + ORM models
- [ ] XNYS calendar (DST, holidays, half-days)
- [ ] Provider protocol, CSV replay provider, bar/quote/corp-action ingestion with quality flags
- [ ] Point-in-time market snapshots, staleness, data hash
- [ ] Order state machine
- [ ] Paper fill model (latency, spread, slippage, participation, partial fills, fees)
- [ ] Paper venue + PaperBroker adapter + fault injection
- [ ] Ledger, settlement, buying power, valuation, corporate actions
- [ ] Risk engine (all checks, boundary tests)
- [ ] Risk decision recording, halts, session state
- [ ] Execution manager (sole intent/order writer)
- [ ] Reconciliation (UNKNOWN resolution, cash/position/fill invariants)
- [ ] Paper tick processor, calendar-aware scheduler, worker handlers, deterministic replay, restart chaos test
- [ ] Read API (portfolio, positions, orders), OpenAPI export, README, architecture diagram, runbooks, security notes

### Plan B — Quant
- [ ] `SignalStrategy` protocol, signal schema validation (TTL, snapshot ID, version)
- [ ] ORB v1 (15-min opening range, volume confirmation, spread/liquidity filter, one entry per symbol per direction per session)
- [ ] VWAP reversion v1 (session VWAP, rolling-vol z-score, reversion evidence, regime gate)
- [ ] `CapitalAllocator`: sleeve budgets (C1), netting of opposing intents, attribution, `allocation_decisions`, `strategy_attribution`
- [ ] Notional → quantity sizing (floors to the increment)
- [ ] EOD flattening of intraday positions, best effort, with unresolved positions recorded
- [ ] Backtest engine reusing the tick processor; train/validation/test split, walk-forward, sensitivity, multiple-hypothesis correction, experiment registry
- [ ] Performance: equity curve, net PnL, drawdown, win rate, expectancy, fill ratio, costs, benchmark (SPY buy-and-hold)
- [ ] `/v1/signals`, `/v1/strategies`, `/v1/performance`, `propose-config` + approval endpoint (applied next session)

### Plan C — Agents and ChatGPT
- [ ] `ResearchAgent` protocol, agent output contract, `agent_runs`, TTL, snapshot IDs; no broker access (enforced by import boundary test)
- [ ] Macro agent: regime enum + risk multiplier in [0,1], which can only reduce exposure; stale → no new entries for dependent strategies
- [ ] Technical agent: parameter proposals and notes only
- [ ] Fundamental watchlist + dated catalysts; Consensus shadow with counterfactual P&L; Evaluator daily attribution/drift
- [ ] Management MCP: `system_status`, `portfolio_summary`, `list_positions`, `list_orders`, `strategy_performance`, `risk_report`, `decision_trace`, `daily_report`, `pause_paper_trading`, `propose_strategy_config`. No order/live/risk-disable/SQL tools.
- [ ] OAuth for the MCP connector (decision 5)
- [ ] Prompt-injection tests (acceptance test 9)

### Plan D — Observability and forward paper
- [ ] Metrics, dashboards, alert rules (stale data, no heartbeat, rejections, job backlog, unreconciled, loss breach, mode change)
- [ ] EOD report, downloadable audit trail (acceptance test 12)
- [ ] Backup/restore with a tested restore
- [ ] Restart and recovery drills, chaos suite
- [ ] 4–8 weeks of forward paper trading; go/no-go report against the operational and economic gates

### Acceptance test map

| # | Acceptance test | Plan / task |
|---|-----------------|-------------|
| 1 | Clean compose up, healthy without broker credentials | A / Task 12 |
| 2 | Deterministic replay, matching hashes | A / Task 25 |
| 3 | Expired TTL or stale quotes → no order | A / Tasks 21, 25 |
| 4 | Symbol/sector/gross limit → stable reason code | A / Task 21 |
| 5 | Daily loss halt persists across restart | A / Task 22 |
| 6 | Timeout → UNKNOWN → reconcile, no duplicate | A / Task 24 |
| 7 | Partial fills update cash/positions/fees/remaining | A / Task 20 |
| 8 | Opposing strategies → one net order, correct attribution | B |
| 9 | Malicious headline/model output can't change risk/secrets/privileges | C (A / Task 26 proves there is no mutation route) |
| 10 | Read-only MCP reports; can't trade | C |
| 11 | Pause/resume authenticated, audited, idempotent, version-checked | A / Task 11 |
| 12 | Full session: reconciled ledger, report, benchmark, audit download | B + D |
