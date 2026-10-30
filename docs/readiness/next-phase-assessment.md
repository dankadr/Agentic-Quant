# Next-phase readiness assessment (2026-10-29)

This records what the next phase has built, what it has verified, and what evidence is still missing.
It is not a statement that the system is ready to trade. It is the input to the rollout decision.

Branch: `feature/next-phase-paper-trading` (milestones 1–8, one commit each).

## The three independent gates

| Gate | Status | Evidence |
|---|---|---|
| 1. Execution reliability | **Partly verified; not passed** | Unit, integration, replay (25 tests) and chaos suites pass. Docker smoke tests pass. Still open: Alpaca paper acceptance, a restore drill, and role-isolation checks beyond the smoke tests. Docker startup, role isolation and restore drill **not run**. Alpaca paper acceptance **not run** (no paper credentials here). |
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

## Run record (2026-10-30)

- Unit tests: **pass**.
- Integration tests: **pass** (three chunks).
- Replay suite (`tests/replay`, 25 tests together with chaos): **pass**, run file by file.
  The suite is slow: each replay takes about two minutes because a session makes roughly 24,000
  database round trips at about 2.5 ms each over the container network. The earlier "hang" was
  this slowness, not a deadlock. A watcher on `pg_stat_activity` found no blocked session.
- Chaos suite (`tests/chaos`, 3 tests): **pass**.
- Docker: `docker compose build` succeeds; the smoke tests (`pytest -m smoke tests/smoke`) **pass**.
- **Defect found and fixed during the run:** `/v1/system/status` returned 500 in the container
  because the application role could not read `alembic_version` (permission denied). The unit and
  integration tests did not catch it: they connect as the table owner. Migration `0010` grants
  read-only access. Regression coverage that runs as the application role is still missing.
- Not yet run: the restore drill, Alpaca paper acceptance, and an explicit role-isolation test.
