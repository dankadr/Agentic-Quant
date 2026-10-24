# Plan B — Quant (strategies, allocator, flattening, backtest, performance, config proposals)

> Executed inline, task by task, TDD. Spec: `docs/spec/mvp-spec-v0.1.md`; roadmap: `2026-10-20-roadmap-and-spec-review.md`.

**Goal:** deterministic ORB and VWAP strategies produce validated signals; an allocator sizes and nets them into
risk-checked orders with per-strategy attribution; intraday positions are flattened before the close; a backtest
engine reuses the production tick path; performance, benchmark and walk-forward analysis are reported; strategy
parameters change only through reviewed proposals applied next session.

**Constraints (unchanged):** PAPER_ONLY, Decimal money, UTC, no look-ahead, risk engine is the only gate, no
float, no shorts. TQQQ is allowed in the universe by the owner (leveraged, own sector bucket).

## Design decisions

- **D1 Strategy input.** `StrategyInput(snapshot, bars, positions, now, session)`: the immutable `MarketSnapshot`
  plus point-in-time session bars per symbol (`load_session_bars`, same visibility rule as snapshots: bar closed
  AND ingested by `as_of`) and the strategy's own open holdings. Strategies are pure functions of it.
- **D2 Signals.** `SignalProposal` (pydantic, frozen) = spec signal schema (strategy_id/version, symbol, side,
  generated_at, expires_at, snapshot id, target_notional, stop, limit, reason codes). Validated: positive
  notional, expiry after generation (TTL ≤ 5 min), limit/stop sane, side BUY/SELL. Persisted to `signals`.
- **D3 Sleeves (decision C1).** A strategy's `allocation_pct` is the MAXIMUM notional it may hold
  (`pct × equity`). Global risk limits always win. Sizing = min(signal target, sleeve remaining, symbol headroom),
  floored to the quantity increment.
- **D4 Netting + attribution.** Per symbol, signed signal quantities are netted: net BUY → one BUY, net SELL →
  one SELL capped at the held quantity (never short). The order carries `strategy_ids`; fills are split across
  them by recorded weights into `strategy_attribution` (qty, fees, realized P&L). One net order per symbol.
- **D5 Flattening.** `eod_flatten` (a built-in, always-enabled strategy id) sells every held position from
  `flatten_minutes_before_close` (10) using marketable limits; leftovers are already recorded by `session.end`.
- **D6 Backtest.** Multi-day replay through the production tick path on a scratch DB with a persistent account;
  walk-forward folds (train/validation/test, test untouched), parameter sensitivity grids, experiment registry
  (`experiments` table), Holm correction across the hypotheses tested.
- **D7 Config proposals.** `propose-config` stores a PENDING proposal (never edits params); a separate approve
  endpoint marks it APPROVED; `start_session` applies approved proposals (version bump) at the next session.

## Tasks

1. Config: strategy `params`, sleeve split (orb 0.20 / vwap 0.15 / eod_flatten 0.00), strategies seeded to DB.
2. `quant/models.py` (`SignalProposal`, `StrategyInput`, `Holding`) + `SignalStrategy` protocol + validation tests.
3. `quant/bars.py` `load_session_bars` (look-ahead guarded) + `quant/holdings.py`.
4. `quant/indicators.py` (VWAP, rolling std, ATR) — Decimal, pure.
5. `quant/orb.py` ORB v1.
6. `quant/vwap.py` VWAP reversion v1.
7. Schema 0003: `strategy_config_proposals`, `experiments`; attribution columns; `ProposedOrder.strategy_ids`.
8. `quant/allocator.py`: sizing, netting, attribution weights, `allocation_decisions`.
9. Attribution on fills (`apply_fill` → `strategy_attribution`).
10. `quant/flatten.py` + `StrategyIntentSource` (strategies → signals → allocator → proposals) wired in the tick.
11. Replay/backtest with strategies: multi-day engine, synthetic multi-day fixtures.
12. `quant/performance.py` + benchmark; walk-forward, sensitivity, experiment registry, Holm.
13. API: `/v1/signals`, `/v1/strategies`, `/v1/performance`, propose + approve config; applied at session start.
14. Docs, OpenAPI snapshot, whole-branch review, merge.

## Review Focus

1. Look-ahead: a strategy must never see a bar that had not closed/ingested at `as_of` (tested at the boundary).
2. Opposing signals from two strategies must net to one order and never create a short.
3. Quantity always floors to the increment; sleeve and symbol caps cannot be exceeded by rounding.
4. Flattening must not be blocked by the entry cutoff, and must not fight a halt (exits stay allowed).
5. A config proposal can never change parameters mid-session or without approval.
