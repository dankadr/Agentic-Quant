# Paper Short Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support explicit short entry and cover throughout the PAPER_ONLY simulation, behind a default-off flag.

**Architecture:** Persist a position effect separately from BUY/SELL. Use signed positions, absolute exposure, restricted short proceeds and fully collateralized paper capacity. Retain advisory-only agents and use mirrored deterministic strategy rules for bearish entries.

**Tech Stack:** Python 3.12, SQLAlchemy, PostgreSQL/Alembic, FastAPI, vanilla JavaScript, pytest.

**Spec:** docs/superpowers/specs/2026-11-21-paper-short-design.md

## Global Constraints

- PAPER_ONLY; no live trades or real borrow availability claims.
- `allow_short` defaults false. Covers remain allowed when disabled.
- Existing account-wide/sector/symbol caps apply to absolute exposure, including pending entries.
- No crossing zero or opposing strategy entries in an already held/reserved symbol.
- Collateral is `max(remaining entry basis, abs(quantity) * current ask)`; short proceeds are restricted separately.
- Existing uncommitted changes in the original checkout must be preserved. Work here on latest main; no production deployment during implementation.
- Keep long-only legacy inputs compatible: missing position effect means BUY opens long, SELL closes long.
- Use isolated test databases only, never the running trading database. Testcontainers is configured.

## Review Focus

- Flag disabled with an existing short: cover/reconciliation/flatten still work (Tasks 1/2).
- Pending partial fills and cancel races: reserve remaining entry/exit quantity until terminal reconciliation (Tasks 1/2).
- Missing short marks: reject valuation instead of silently dropping the liability (Task 1).
- Legacy intent replay: missing effect retains long-only interpretation (Tasks 1/2).
- Opposing strategy entry and same-symbol entry/exit: reject accidental direction changes and preserve attribution (Task 2).

---

### Task 1: Explicit effects, signed accounting and collateralized paper venue

**Files:**
- Modify: `src/trading/core/{enums,config}.py`, `src/trading/brokers/base.py`, `src/trading/brokers/paper/broker.py`, `src/trading/db/models/{trading,paper}.py` (locate actual venue-model file), `src/trading/portfolio/{ledger,balances,valuation,corporate_actions,attribution}.py`, `src/trading/execution/intents.py`, `src/trading/risk/models.py`, `src/trading/quant/models.py`.
- Create: next Alembic migration `0013_paper_shorts.py` and focused accounting/venue/effect tests under existing unit/integration suites.

**Interfaces:**
- Produce `PositionEffect` enum in core/enums with `OPEN_LONG`, `CLOSE_LONG`, `OPEN_SHORT`, `CLOSE_SHORT`; properties `side: Side`, `is_entry: bool`, `is_short: bool`. Produce `effective_effect(side: Side, effect: PositionEffect | None) -> PositionEffect` with legacy fallback and mismatched-side rejection.
- Add optional defaulted `position_effect: PositionEffect | None = None` to `BrokerOrderRequest`, `ProposalRequest`, `ProposedOrder`, `SignalProposal`. Signal validation checks effect/side consistency and only entry-stop coherence (long stop below, short stop above). Persist nullable effect on intent, order and venue order; null means legacy. Default omission remains compatible with tests/fixtures.
- `RiskPolicy.allow_short: bool = False` replaces Literal false. PaperBroker constructor accepts keyword `allow_short: bool = False`; covers do not depend on enablement. Downstream factory wiring is Task 2.
- Append default-zero `short_collateral` and `restricted_short_proceeds` to PortfolioState; buying power deducts both and pending reservations. Keep existing positional constructor compatibility.
- Leave strategy/risk/execution propagation and dashboard for later tasks; do not silently infer short trades from unannotated SELLs.

- [ ] Write failing tests: sell 10 at 100 opens qty -10, partial cover 4 at 90 realizes 40 less fees, final cover closes; short proceeds do not grow buying power; adverse ask raises collateral; no mark raises MissingMark; no cash or accidental crossing rejected; a disabled venue rejects new shorts but accepts covers.
- [ ] Run focused tests to confirm expected failures before implementation.
- [ ] Implement effect types and backward-compatible migration with constraints on allowed effects, valid side/effect pairs and signed position quantities. Migration downgrade refuses non-flat shorts and explicit short history it cannot faithfully downgrade. Preserve order/fill positive quantities and existing position basis.
- [ ] Implement signed ledger/venue positions, correct weighted basis and realized P&L for increases/reductions, idempotency, no direction crossing. Apply pending order collateral and cover reservations in submit/fill capacity checks, with account serialization. Ensure venue never drops a liability or releases cover capacity early. Include adverse-market fill-time capacity recheck.
- [ ] Implement valuation, restricted funds and corporate actions for signed quantities. Dividend cashflows debit shorts; splits preserve signed quantities/basis. Keep attribution signed by BUY/SELL.
- [ ] Run focused integration tests, unit suite, lint/type checks for changed files; report exact results and any baseline failures.
- [ ] Commit only Task 1 files and write task report.

### Task 2: Risk, deterministic bearish strategies, allocation and execution integration

**Files:**
- Modify: `src/trading/risk/{engine,models}.py`, `src/trading/execution/{intents,manager,tick,reconcile,controls}.py`, `src/trading/quant/{allocator,source,models,orb,vwap,flatten,holdings,roundtrips,performance,backtest}.py`, `src/trading/services/{worker,heavy}.py` and actual broker-construction sites, and related unit/integration/replay tests.
- Create focused short integration/replay tests and collateral-deficit control helper if needed.

**Interfaces:**
- Consume Task 1 effects and PortfolioState collateral fields. Propagate explicit effect into persisted order requests and venue calls, without changing historical legacy interpretation.
- Add default `allow_short: bool = False` to StrategyInput and AllocationContext and wire from config.risk. Existing strategies remain long-only with false. Pending short entries/covers must have distinct maps and enter the same caps as pending long entries; expose defaulted AccountView/AllocationContext fields as needed.
- Existing `_entry` long rules remain. ORB short rule: close below range-low times `(1-buffer_bps/10000)`, same volume/spread/session/reentry checks; stop at range-high, target entry minus `target_r * (range_high-entry)`; exit at stop, target or time stop. SELL entry limit uses bid with existing downward padding; BUY cover limit uses ask with upward padding.
- VWAP short mirrors its existing long deviation/confirmation exactly: positive deviation above VWAP triggers a bearish mean-reversion candidate with mirrored confirmation, stop above entry, cover on VWAP/stop/time. Keep existing indicator sample requirements.
- Holdings and entry history use signed nonzero quantities; pending entries count by effect, not BUY. Closed roundtrip P&L handles short episodes and fees exactly once.

- [ ] Write failing strategy/risk tests for bearish entry and buy-to-cover, long unchanged with flag off, flag-off cover, short stop ordering, pending short exposure and pending covers, risk-reducing cover under halt, opposite strategy rejection, stale data, and no zero-crossing.
- [ ] Run to verify expected failures.
- [ ] Implement generalized allocation and effect propagation. Never net opposing directions/entry-exit into an ambiguous order. Exits get priority, entries wait. Honor existing sleeve allocations and macro scale for both entry directions.
- [ ] Replace BUY-as-entry and qty>0 assumptions throughout entry expiry, flatten, reconciliation, session-end, data collection, marks and controls. Do not cancel covers as stale entries. Preserve reservations through fill reconciliation. Wire allow_short only into the in-process paper venue; reject explicit shorts in external broker adapters unless separately supported (no real/external broker short enablement in this scope).
- [ ] Add collateral-deficit control: cancel pending entries and request risk-reducing covers on fresh tradable data, report incident when free capital is negative. Cover uses restricted funds and does not require fresh free cash reserved for an entry.
- [ ] Add full isolated-DB tests through strategy/source → risk → manager → venue → ledger → reconciliation with falling and rising prices, partial cover, restart and EOD flatten. Assert exact resulting qty, equity, realized P&L and restrictions.
- [ ] Run all unit tests plus focused execution/portfolio/risk/quant/replay integration suites, fix regressions, lint/type-check.
- [ ] Commit Task 2 and report exact RED/GREEN commands/results.

### Task 3: Dashboard visibility, operational docs and final verification

**Files:**
- Modify: `src/trading/api/routes/{dashboard,portfolio,transactions,system}.py`, `src/trading/api/static/{views,util,style}.js` (use actual style.css), `src/trading/ui/templates/overview.html` if used, `configs/risk.yaml`, `README.md`, `docs/runbooks/` relevant files; focused API/e2e tests. Modify any remaining analytics readers discovered by signed-position audit.

**Interfaces:**
- Consume persisted position effects, signed quantities and collateral fields. API fields remain backward compatible and additive. Show LONG/SHORT direction, position size, entry, short liability, collateral, restricted proceeds, P&L, and explicit short flag state. Label BUY to cover versus BUY to open and SELL to open versus SELL to close.
- Use established UI conventions. Explain paper borrow assumption and omitted borrow fees briefly; do not expose implementation details.

- [ ] Write failing API tests for a signed short position and explicit opening/cover trade labels; verify existing long responses stay compatible.
- [ ] Implement API/dashboard fields/labels and correct any remaining performance/report readers that ignore negative holdings. Add a brief paper assumption note and default-off flag documentation.
- [ ] Test browser dashboard on mobile viewport with an isolated populated short fixture (never production data writes), capture/review screenshot and verify long/short labels, P&L and collateral.
- [ ] Run bare `uv run pytest` on isolated testcontainers DB, `uv run ruff check .`, `uv run mypy src`; report every failure/skipped check. Run relevant browser e2e tests and migration upgrade safety tests.
- [ ] Document exact deployment steps, backup/migration sequence, acceptance probes and how to enable/disable after separate rollout review. Leave running production stack unchanged and allow_short false in checked-in config.
- [ ] Commit Task 3 and report full verification evidence for final review.
