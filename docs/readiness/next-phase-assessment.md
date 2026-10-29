# Next-phase readiness assessment (2026-10-29)

This records what the next phase has built, what it has verified, and what evidence is still missing.
It is not a statement that the system is ready to trade. It is the input to the rollout decision.

Branch: `feature/next-phase-paper-trading` (milestones 1–8, one commit each).

## The three independent gates

| Gate | Status | Evidence |
|---|---|---|
| 1. Execution reliability | **Not yet passed** | Unit and integration suites pass. Replay and chaos suites are not verified (a replay test blocks on a database connection; see the run record). Docker startup, role isolation and restore drill **not run**. Alpaca paper acceptance **not run** (no paper credentials here). |
| 2. Research quality | **INCONCLUSIVE** | The evaluation harness and verifier are built and tested. The evaluation set contains **4 synthetic cases only**; the plan requires at least 40 real documents, 10 per category, and human review of sampled claims. |
| 3. Strategy evidence | **INCONCLUSIVE by construction** | Holdout ledger, coverage rules and verdicts are built and tested. **No comparative run has been performed.** The plan requires at least 60 usable sessions. |

No gate has passed. The plan's rule holds: the strategy gate may legitimately remain inconclusive
and must not be presented as proven profitability.

## Rollout sequence (plan §3, Milestone 8)

1. Deploy worker isolation, controls and monitoring against the internal simulator — **code
   complete; deployment not yet run**.
2. Enable research in shadow and validate report quality — **blocked on a real evaluation set**.
3. Connect Alpaca in read-only mode and establish account reconciliation — **adapter and read-only
   gate built; run `trading admin alpaca-readonly-check` against a paper account to proceed**.
4. Enable controlled Alpaca paper orders — **not started**. Requires the broker-neutral tick
   refactor, which is not done (the live loop still uses the internal simulator).
5. Run at least 20 consecutive market sessions meeting the operational gate — **not started**.
   `trading admin eval-rollout` evaluates the session records.

## Known gaps that block the rollout

- The execution tick is not yet broker-neutral; the Alpaca adapter is not in the live loop.
- Capital rules (lower of strategy and broker equity) exist as tested functions and are not applied
  in the risk engine.
- Exit repricing (30 s) and market escalation (120 s) are not implemented (Milestone 2).
- Technical-agent proposals are not yet tied to the effective configuration and dataset manifest
  (Milestone 3).
- Scheduled EDGAR polling and macro release calendars are not wired (Milestone 5).
- Backup and restore scripts have not run against a live deployment; the second-host monitor is not
  provisioned (Milestone 6).
- Telegram delivery is tested against a fake, not the live API.

## Run record (2026-10-29)

- Unit tests: **pass** (exit 0).
- Integration tests: **pass**, in three chunks (exit 0 each).
- Replay and chaos suites: **NOT VERIFIED.** `tests/replay/test_replay_determinism.py` passes its
  first three tests. `test_session_end_records_unresolved_positions` blocks indefinitely while opening
  a database connection (a stack dump shows psycopg waiting on the server during SQLAlchemy's
  connect-time ping). Its cause is not yet identified. The chaos suite and the remaining replay
  files have not been run to completion. The plan requires these suites to pass before the release,
  so this is a blocking item for Gate 1.
- Docker startup, role isolation, migration-and-restore drill: not run in this environment.
