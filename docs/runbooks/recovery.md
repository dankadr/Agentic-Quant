# Recovery

All recovery is **fail closed**: the system will not place an order until it can prove its books
match the broker.

## Worker restarts or is replaced

1. The new worker acquires the executor lease. Taking over bumps the **fencing token**, so a hung
   old process fails its next write with `LeaseLost` and exits with code 3.
2. Before any job runs, the worker reconciles (startup hook). The risk state is `NORMAL` only after
   a clean reconciliation.
3. Check: `GET /v1/system/status` → `risk_state: NORMAL`, `last_reconciled_at` is recent.

## An order is `UNKNOWN` (or stuck `SUBMITTING`)

Reconciliation resolves it by asking the broker. It never resubmits.

- Broker has the order → adopted (`ACKNOWLEDGED`, or `CANCELED`/`EXPIRED` via `ACKNOWLEDGED`).
- Broker has no record → `PENDING` for 30 s (`unknown_order_grace_seconds`), then `REJECTED`
  with reason `NOT_FOUND_AT_BROKER`. Any new attempt needs a fresh risk decision.

## `RECONCILIATION_REQUIRED`

Something did not match: read the latest row of `reconciliation_runs` (`report_json.mismatches`)
and the audit event `reconciliation.run`. Mismatch kinds: `ORPHAN_BROKER_ORDER`, `MISSING_AT_BROKER`,
`FILLED_QTY`, `POSITION`, `CASH`, `LEDGER_INTERNAL`. While in this state **all** orders (entries and
exits) are refused. Resolve the cause (never edit fills or the ledger: corrections are reversal
entries via `reverse_ledger_entry`), then the next clean reconciliation restores `NORMAL`.

## Halts

- **Daily loss** (loss >= 1% of session-start equity): blocks new entries; exits stay allowed. Clears
  automatically when a *new* session starts. A same-day restart does not clear it.
- **Drawdown** (>= 5% below peak): blocks new entries; exits stay allowed. Cleared only by
  `trading admin clear-drawdown-halt --reason "..."` (audited, resets the peak to the latest snapshot).

## Kill switch

`POST /v1/control/pause` (operator key, `Idempotency-Key`) is always allowed. Pausing blocks entries
and exits; resume to flatten.

## Database outage

Readiness returns 503 and the worker exits non-zero (it cannot prove it still holds the lease). After
the database is back the restarted worker acquires the lease and reconciles first.
