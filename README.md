# robinhood-trading

A **paper-only** multi-agent quant trading system. There is no live trading, no broker credential
and no Robinhood integration anywhere in this repository (no supported Robinhood order API has been
verified; see `docs/superpowers/plans/2026-10-20-roadmap-and-spec-review.md` §1.3).

- Master specification: `docs/spec/mvp-spec-v0.1.md`
- Build plans and decisions: `docs/superpowers/plans/`, `docs/decisions/`
- Architecture: `docs/architecture/overview.md` · Security: `docs/security/notes.md`
- Runbooks: `docs/runbooks/` · API contract: `docs/api-contracts/openapi.json`

**Status (Plans A–B):** infrastructure, database, durable jobs, fenced leases, audit, API, paper
broker, ledger, reconciliation, risk engine, calendar-aware scheduling and deterministic replay
(Plan A) plus the quant layer (Plan B): ORB and VWAP-reversion strategies, a sleeve allocator that
nets signals into risk-checked orders with per-strategy attribution, end-of-day flattening, a
multi-day backtest engine, performance reporting against a benchmark, walk-forward/sensitivity
research tooling, and a reviewed strategy-config proposal workflow. Research agents and the ChatGPT
MCP come in Plans C–D.

## Requirements

Python 3.12, [uv](https://docs.astral.sh/uv/), Docker + Docker Compose v2.

## Setup

```bash
uv sync                                   # install pinned dependencies (uv.lock)
./scripts/init-secrets.sh                 # random DB passwords in ./secrets (never overwritten)
docker compose up -d --build              # postgres -> migrate -> api, worker, scheduler, prometheus
curl -s localhost:8000/v1/health/ready    # database ok, executor_lease held
docker compose run --rm api admin create-api-key --name me --role operator   # prints the key ONCE
```

The stack starts **paused** with risk state `UNKNOWN` (fail closed). `market_data` reads
`not_configured` until a licensed provider is chosen (open decision 1), and ticks are no-ops until then.

## Test

```bash
make lint type test        # ruff, mypy --strict, pytest (integration tests need a Postgres, below)
make smoke                 # docker compose end-to-end check (acceptance test 1)
```

Integration, replay and chaos tests use a real PostgreSQL. By default they start one with
testcontainers (needs a running Docker). To use an existing server instead:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://user@host/dbname'   # a scratch database
uv run pytest
```

The full suite takes a few minutes because it replays whole synthetic sessions.

## Replay a recorded session (deterministic)

Replay takes the executor lease, so run it on a **scratch database**, never next to a live worker:

```bash
export TRADING_DATABASE_URL='postgresql+psycopg://user@host/scratch'
uv run trading migrate
uv run trading replay \
  --fixtures tests/fixtures/replay/2026-10-19 --date 2026-10-19 \
  --script tests/fixtures/replay/scripts/basic.yaml
```

It prints `result_hash`, order/fill counts and final equity. Running it twice on clean databases
prints the same hash. The fixtures are **synthetic** (see `tests/fixtures/replay/2026-10-19/README.md`).

## Backtest and research

`trading backtest` replays several sessions through the production tick path with one persistent
paper account (also **scratch database only**: the database name must contain `scratch`, `test`, `replay` or
`backtest`, and it refuses a database holding any non-replay/backtest account or API principal;
`--reset` wipes it):

```bash
uv run trading backtest --fixtures DIR --start 2026-10-19 --end 2026-10-23 --reset
```

`trading.quant.research` provides `parameter_grid`, `walk_forward` (parameters chosen on train
windows only, scored once on the next test window, final holdout never touched), `sensitivity`
and `record_experiment` (every hypothesis is recorded, rejected ones included); p-values use a
one-sided t-test with Holm correction. `backtest_evaluator` plugs a real backtest into them.
Multi-day **synthetic** fixtures: `scripts/make_synthetic_fixtures.py --days N`.

## Strategy parameters

Parameters live in the `strategies` table and change only through
`POST /v1/strategies/{id}/propose-config` (stored `PENDING_REVIEW`), a review by a *different*
operator (`POST /v1/config-proposals/{id}/approve|reject`), and application at the **next session
start** (version bump, audited). Read endpoints: `/v1/strategies`, `/v1/signals`,
`/v1/performance`, `/v1/config-proposals`.

## Market data (Alpaca, free IEX feed)

Put the Alpaca **market-data** key pair (a paper account's keys work; never trading keys) in
`secrets/alpaca_key_id` and `secrets/alpaca_secret` (`scripts/init-secrets.sh` creates empty
placeholders; empty means "not configured"). The adapter only talks to `data.alpaca.markets`
(it refuses any other host) and has no order or account calls.

```bash
uv run trading alpaca-check --start 2026-10-23T14:30:00Z --end 2026-10-23T14:45:00Z   # no database
uv run trading ingest-alpaca --start 2026-10-23T13:30:00Z --end 2026-10-23T20:00:00Z  # backfill
```

With keys present the worker ingests live bars and quotes each minute and trades **paper only**.
IEX is a single venue, so bar volume is a small fraction of consolidated volume (thinly traded
symbols such as DIA can fail the ORB `min_avg_volume` filter, by design). Quotes are the last quote
per symbol-minute, one request each. Corporate actions are not fetched.

## Research agents (optional, off by default)

`configs/agents.yaml` configures LLM agents that **only propose**: they have no tools and cannot
place orders, change risk limits or switch mode. Their replies are parsed as JSON and validated
against strict schemas; anything else is stored as `REJECTED_INVALID`. The endpoint is
OpenAI-compatible (your Unsloth server); put its key in `secrets/llm_api_key` (empty = off).

- **Macro** (`agents.macro.enabled`): from numeric index/breadth features it classifies the regime
  and proposes a `risk_multiplier` in [0, 1] with a TTL. It can only shrink new-entry size of
  `dependent_strategies`; with `required: true` a missing/expired view blocks their new entries
  (exits are never blocked). Runs every 30 minutes after the opening range.
- **Evaluator** (`agents.evaluator.enabled`): a daily narrative over `trading report`, stored in
  `agent_runs`.

Not built yet: technical, fundamental and consensus agents.

## Paper run

```bash
KEY=...   # operator key from create-api-key
curl -s -H "Authorization: Bearer $KEY" localhost:8000/v1/system/status          # note "version"
curl -s -X POST localhost:8000/v1/control/resume-paper \
  -H "Authorization: Bearer $KEY" -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' -d '{"reason":"start paper session","expected_version":2}'
```

`resume-paper` refuses with `503 NOT_READY` (and a `checks` object) unless the database is up, the
executor lease is held, the risk state is `NORMAL` and market data is not stale. Live-hours paper
trading additionally needs a market data provider adapter.

## Useful commands

| Command | Purpose |
|---------|---------|
| `uv run trading openapi > docs/api-contracts/openapi.json` | regenerate the API contract (a test checks it is current) |
| `uv run trading admin create-api-key --name N --role viewer\|operator` | create an API key (printed once, stored hashed) |
| `uv run trading admin clear-drawdown-halt --reason "..."` | explicit, audited clearing of a drawdown halt |
| `uv run trading backtest --fixtures DIR --start D --end D` | multi-day backtest (scratch DB) |
| `uv run trading report --date D` | end-of-day report: data coverage, strategies, costs, P&L |
| `uv run trading ingest-csv --path DIR --start ISO --end ISO` | load CSV bars/quotes into the database |

## Known limitations

- Market data comes from Alpaca's free IEX feed only (see above); there is no consolidated (SIP) data.
- Corporate actions are ingested and the ledger can apply them, but the paper venue does not model
  them, so they are not applied automatically (it would break reconciliation).
- Synthetic backtests prove plumbing, not profitability; the scripted intent source remains for tests.
- TQQQ (leveraged) is in the universe at the owner's request, in its own sector bucket; it is
  outside the spec's "no leverage" intent and is capped by the same risk limits.
