# Deterministic replay

`trading replay` runs one session through the production path (ingest -> point-in-time snapshot ->
paper broker -> risk -> execution -> reconciliation) on a `ManualClock`.

## Rules

- Use a scratch database; replay takes the executor lease.
- Fixtures are CSV with UTC-offset timestamps. Naive timestamps are rejected with file and line.
- The result hash covers orders, fills, ledger amounts and final equity. UUIDs, row ids and
  wall-clock times are excluded, so two runs on clean databases hash identically.
- `stop_after` / `resume` simulate a crash: a resumed run reconciles first and must finish exactly
  as an uninterrupted run (tested in `tests/chaos/test_restart_mid_session.py`).

## Scripts

`tests/fixtures/replay/scripts/*.yaml` list proposals: `at`, `symbol`, `side`, `quantity`,
`order_type`, `limit_price`, `strategy_id`, `ttl_seconds`. Limits are ceilings/floors: the paper broker
fills at the quote plus slippage, never at the limit or at a bar high/low.

## Look-ahead guard

A bar is visible only after it closes **and** was ingested by `as_of`; a quote only if it existed and
was ingested by `as_of` (`build_snapshot`). That is what makes replay and live paper trading identical.
