# Security notes

- **No execution credentials exist.** There is no broker adapter other than `PaperBroker`. Only the
  worker constructs it; the API process cannot import it (tested, including transitively).
- **Secrets** come from mounted files (`*_FILE`), never environment values or logs. Log redaction masks
  sensitive keys and `Bearer`/`trk_` patterns, including inside nested objects; the same scrub is applied
  to audit details and persisted job errors.
- **Database roles.** `trading_owner` runs migrations; `trading_app` has DML only (no TRUNCATE, no DDL).
  Triggers make `fills`, `cash_ledger`, `order_intents`, `order_events`, `risk_decisions`,
  `audit_events` and `paper_venue.venue_fills` append-only for every role.
- **Authentication.** Bearer API keys (256-bit random, stored as SHA-256), roles `viewer`/`operator`.
  Mutations need the operator role and an `Idempotency-Key`; every control action is audited with actor
  and request id.
- **Network.** The API is published on 127.0.0.1 only; expose it with `tailscale serve`, never
  `tailscale funnel`. Metrics ports (9100-9102) are not published to the host.
- **Fencing.** Every worker write asserts the executor lease token inside its transaction, and again
  immediately before a broker call and before any venue mutation. Lease holders are unique per process
  (`<worker_id>:<pid>:<nonce>`), so two processes sharing a worker id cannot share a lease. A handler
  that hits `LeaseLost` or a database error stops the worker (exit 3) instead of recording a job failure.
- **Orders are bound to their decision.** The execution manager re-checks the persisted decision
  (allowed, same account/instrument/snapshot/dedupe key, **identical order terms**, not older than 5 min).
- **Fail closed.** Missing data, unknown risk state, reconciliation mismatch, calendar gaps and database
  errors all block new orders.
- **Untrusted input.** The risk engine re-derives data freshness from decision time and does not trust a
  snapshot's own verdict. Agent output (Plan C) will be schema-validated and can only propose.
- **No live path.** `mode` is `Literal["PAPER_ONLY"]`; `accounts.mode` is CHECK-constrained; margin,
  short, options and crypto flags are `Literal[False]`.

## Known gaps (tracked)

- The API, worker and scheduler share one database role (`trading_app`); the code boundary is enforced
  by tests (the API/scheduler entrypoints never load broker code) but a per-service DB role split is
  still to do (Plan D hardening).

## Open items

Secrets review and backup/restore drills are part of the operational gate (Plan D). The Compose stack,
role separation (owner vs app), migrations and operator flow are exercised against real containers by
`make smoke` (`tests/smoke`).
