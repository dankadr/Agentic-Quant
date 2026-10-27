# Startup, shutdown and recovery

## Startup (PAPER_ONLY)

1. `./scripts/init-secrets.sh` — creates database passwords (never overwrites).
2. `docker compose up -d --build` — `postgres` → `migrate` (owner role, applies Alembic) → `api`, `worker`,
   `scheduler`, `prometheus`. The stack starts **paused** with risk state `UNKNOWN` (fail closed).
3. `curl -s localhost:8000/v1/health/ready` — expect `database: ok` and `executor_lease: held`.
   `market_data: not_configured` is expected until a licensed provider is chosen.
4. Verify the mode: `GET /v1/system/status` must show `"mode": "PAPER_ONLY"`.
5. Calendar and data freshness: confirm a trading session is expected today and data is fresh
   (`last_data_at`). The worker reconciles cash and positions on start (added with Task 25); the risk
   state becomes `NORMAL` only after a clean reconciliation.
6. Only then: `POST /v1/control/resume-paper` (operator key, `Idempotency-Key`, current `expected_version`).
   It refuses with 503 `NOT_READY` and a `checks` object while anything is unhealthy.

## Shutdown

1. `POST /v1/control/pause` — stops new entries.
2. `docker compose stop worker scheduler` — SIGTERM: the worker finishes its current job, releases its lease and
   exits 0. In-flight orders are left for reconciliation on the next start.
3. `docker compose stop api postgres`.

## Exit codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | clean stop | none |
| 1 | startup failed (e.g. reconciliation error) | read logs; the lease was released |
| 3 | lease lost or database failure | the supervisor restarts it; it reconciles before processing |
| 4 | an approved agent-setting change was applied | expected: the supervisor restarts it with the new settings (see the README Agents view) |

## Recovery

A restarted worker acquires the executor lease (bumping the fencing token, so any stale process can no
longer write), reconciles, and only then processes jobs. Full procedures are in `recovery.md` (Task 26).
