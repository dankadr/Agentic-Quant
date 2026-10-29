# Controlled paper rollout

Do not begin this runbook until `docs/readiness/next-phase-assessment.md` shows the gates it depends
on as passed. Live trading is out of scope for this phase and no switch enables it.

## Stages

1. **Internal simulator, deployed.** Start the stack with `make up` from the release checkout. Confirm
   `/v1/system/status` shows the expected revision and matching `schema_applied` and `schema_head`.
2. **Research in shadow.** Enable analysis, run `trading admin eval-research --dataset <real set>
   --supported-claim-rate <reviewed rate>`. Proceed only on `release: PASS`.
3. **Alpaca read-only.** Bind the paper account (`TRADING_ALPACA_PAPER_ACCOUNT`), mount the paper
   credentials into the execution service only, then run `trading admin alpaca-readonly-check`.
   Proceed only on `gate: PASS` and every symbol `ok`.
4. **Alpaca paper orders.** Requires the broker-neutral tick refactor. Enable with
   `TRADING_ALPACA_PAPER_ENABLED=true`. Verify fractional-limit behaviour in the paper environment
   before setting `TRADING_ALPACA_FRACTIONAL_LIMIT_VERIFIED`.
5. **Operational gate.** Record one row per session (open positions at close, unexplained
   discrepancies, duplicate executions, uncontrolled orders, trades) in a JSON file and run
   `trading admin eval-rollout --records <file> --acceptance-passed` only once the paper acceptance
   scenarios have all passed. The gate needs 20 consecutive clean sessions.

## Stop conditions

Stop entries immediately (`POST /v1/control/stop-entries`) on: an unresolved position at close, an
unexplained discrepancy, a duplicate execution, or an order whose lifecycle the system cannot explain.
Then reconcile before any further session.

## Not part of this phase

Live-money trading; any switch that enables it; automatic promotion of an agent's trading influence.
