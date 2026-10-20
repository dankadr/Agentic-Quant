# Plan A — Foundation, Paper Execution & Risk Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A PAPER_ONLY trading backbone that starts cleanly under Docker Compose and covers spec Sprints 1–2 plus the risk engine pulled forward from Sprint 3. It ingests and replays minute bars, risk-checks proposed orders, simulates fills realistically, keeps a reconciled ledger, survives restarts without duplicate orders, and is operated through an authenticated, audited API.

**Architecture:** One Python package (`trading`) with three entrypoints (`api`, `worker`, `scheduler`) sharing PostgreSQL as the only source of truth. The worker holds a fenced executor lease and is the only process that touches `BrokerAdapter`. The paper broker is modelled as an external venue with its own `paper_venue` schema, so that reconciliation is meaningful. Every money/quantity value is `Decimal`, every timestamp is tz-aware UTC, and every time-dependent component takes an injected `Clock`.

**Tech Stack:** Python 3.12, uv (lockfile), FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2.0 (sync, typed `Mapped[]`), psycopg 3, Alembic, PostgreSQL 16, structlog, prometheus-client, exchange-calendars, typer, PyYAML. Tests use pytest, hypothesis, httpx, and testcontainers[postgres]. Linting and types use ruff and mypy --strict. Docker Compose v2.

**Spec:** `docs/spec/mvp-spec-v0.1.md`. Conflict resolutions and schema additions are in `docs/superpowers/plans/2026-10-20-roadmap-and-spec-review.md` §1, referenced below as C1–C10. Read both.

## Global Constraints

- Mode is `PAPER_ONLY`. No live adapter, no broker credentials, no Robinhood API code. `accounts.mode` has a CHECK so it can only be `'PAPER'`. Config `mode` is `Literal["PAPER_ONLY"]`.
- Python 3.12; PostgreSQL 16; FastAPI; Docker Compose.
- `Decimal` for money and quantity; never `float`. DB: `NUMERIC(20,8)` for quantities/prices, `NUMERIC(20,4)` for currency amounts.
- All times are tz-aware UTC internally. Naive datetimes are rejected (`NaiveDatetimeError`). The exchange calendar is XNYS in `America/New_York`.
- API: `/v1` namespace, JSON, ISO-8601 UTC timestamps (`Z` suffix), opaque UUID identifiers, `X-Request-ID` correlation, cursor pagination, RFC 7807 Problem Details. HTTP 409 = stale version/conflict, 422 = invalid configuration/input, 503 = unavailable state.
- POST mutations require the authenticated `operator` role and an `Idempotency-Key` that is unique per principal+route.
- Risk policy values (verbatim): starting cash `1000.00`; gross ≤ `0.80`; per-symbol ≤ `0.10`; per-sector ≤ `0.30`; daily loss ≤ `0.01` of session-start equity; drawdown ≤ `0.05`; max `10` new entries/day; max `3` open symbols; cash reserve ≥ `0.20`; no margin, short, options, or crypto; stale bars > `90` s are rejected; no new entries in the last `15` minutes of the regular session.
- Only the worker process constructs a `BrokerAdapter`. `trading.api` must never import `trading.brokers` (enforced by a test in Task 26).
- Fills, cash ledger, audit events, order intents, order events, and risk decisions are immutable. Corrections are made only through reversal entries.
- Secrets come from mounted files (`*_FILE` settings) and are never logged.
- Fail closed: any ambiguity (missing data, mismatch, unknown state) blocks new orders.
- No `datetime.now()`/`time.time()` outside `trading.core.clock.SystemClock`. No unseeded randomness.

## Review Focus

1. **The worker dies after committing `SUBMITTING` but before the broker call returns.** Expected: restart reconciliation adopts the broker's record, or after a 30 s grace marks the order `REJECTED`. It never resubmits. Tested in Task 24 (`test_submitting_crash_*`).
2. **Two workers are alive at once** (a restart overlap, or a hung worker waking up after its lease was taken over). Expected: the stale worker's writes fail with `LeaseLost`, and no order reaches the venue. Tested in Task 7 (`test_stale_holder_fence_fails`) and Task 23 (`test_submit_with_stale_lease_never_reaches_broker`).
3. **Rounding while converting notional to quantity.** Expected: quantity always floors to the instrument increment, so a cap of exactly $100 can never be exceeded by rounding up. Tested in Task 2 (`test_floor_to_increment_never_rounds_up`) and Task 21 (`test_symbol_cap_uses_exact_quantity`).
4. **Naive or local-time timestamps in a CSV, provider, or API input.** Expected: rejected with a clear error, never silently treated as UTC. Tested in Task 2 (`test_ensure_utc_rejects_naive`) and Task 15 (`test_csv_naive_timestamp_rejected`).
5. **Postgres becomes unreachable mid-session.** Expected: readiness returns 503, the worker stops processing and exits non-zero, and no orders are created. After recovery, it reconciles before any entry. Tested in Task 10 (`test_ready_503_when_db_down`) and Task 12 (`test_worker_exits_when_lease_renew_fails`).

---

## File Structure

```
robinhood-trading/
  pyproject.toml  uv.lock  Makefile  Dockerfile  docker-compose.yml  .env.example  .gitignore  README.md
  .github/workflows/ci.yml
  alembic.ini
  configs/{system.yaml,risk.yaml,strategies.yaml,paper_broker.yaml}
  deploy/{postgres/init-roles.sh,prometheus/prometheus.yml}
  scripts/{init-secrets.sh,make_synthetic_fixtures.py}
  migrations/{env.py,versions/0001_ops.py,versions/0002_trading.py}
  src/trading/
    __init__.py  cli.py
    core/{money.py,time.py,clock.py,ids.py,codes.py,enums.py,hashing.py,config.py,errors.py}
    monitoring/{logging.py,metrics.py,audit.py}
    db/{base.py,engine.py,migrate.py,seed.py,models/{ops.py,market.py,trading.py,portfolio.py,research.py,venue.py}}
    jobs/{queue.py,runner.py,lease.py,outbox.py,schedule.py}
    control/state.py
    api/{app.py,deps.py,problems.py,middleware.py,auth.py,idempotency.py,pagination.py,
         routes/{health.py,system.py,control.py,audit.py,portfolio.py}}
    data/{calendar.py,models.py,instruments.py,provider.py,replay_provider.py,ingest.py,snapshot.py}
    brokers/{base.py,paper/{fill_model.py,broker.py,faults.py}}
    portfolio/{ledger.py,balances.py,valuation.py,corporate_actions.py}
    risk/{models.py,engine.py,service.py,halts.py}
    execution/{state_machine.py,manager.py,reconcile.py,intents.py,tick.py,replay.py}
    services/{worker.py,scheduler.py,handlers.py}
    quant/ agents/ mcp/            # empty packages with __init__.py, filled in Plans B/C
  tests/{conftest.py,unit/,integration/,replay/,chaos/,smoke/,fixtures/replay/2026-10-19/}
  docs/{architecture/,api-contracts/,runbooks/,security/,decisions/}
```

`tests/unit` needs no Docker. Everything in `tests/integration`, `tests/replay`, and `tests/chaos` uses the Postgres fixture. `tests/smoke` drives Docker Compose and is excluded from the default `pytest` run (marker `smoke`).

---

# Sprint 1 — Foundation

### Task 1: Repository scaffold and tooling

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `Makefile`, `.gitignore`, `.env.example`, `README.md`, `.github/workflows/ci.yml`, `src/trading/__init__.py`, `src/trading/cli.py`, empty `__init__.py` in every package listed in File Structure, `docs/decisions/README.md`
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Produces: `trading.__version__: str = "0.1.0"`; a typer app `trading.cli:app`, installed as console script `trading`; `trading version` prints `0.1.0`.

- [ ] **Step 1: Install uv** if it is missing: `brew install uv` (macOS) or `curl -LsSf https://astral.sh/uv/install.sh | sh` (Debian). Check with `uv --version`.
- [ ] **Step 2: Write the failing test**

```python
from typer.testing import CliRunner
import trading
from trading.cli import app

def test_version_constant():
    assert trading.__version__ == "0.1.0"

def test_cli_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0 and result.stdout.strip() == "0.1.0"
```

- [ ] **Step 3: Create `pyproject.toml`.** Use `requires-python = ">=3.12,<3.13"`, a src layout with hatchling, runtime deps from the Tech Stack, and a `dev` dependency group (pytest, pytest-cov, hypothesis, httpx, testcontainers[postgres], ruff, mypy, types-PyYAML). Ruff: `select = ["E","F","I","B","UP","S","DTZ","PL","RUF"]`; `DTZ` catches naive datetime calls. mypy: `strict = true`, plugins `pydantic.mypy`. pytest: `testpaths = ["tests"]`, markers `integration`, `smoke`, `addopts = "-m 'not smoke'"`. Run `uv lock && uv sync`.
- [ ] **Step 4: Add `Makefile` targets.** `lint` (ruff check + ruff format --check), `type` (mypy src), `test` (pytest), `smoke` (pytest -m smoke tests/smoke), `up`, `down`. Add `.gitignore` (`.venv`, `__pycache__`, `secrets/`, `.env`), an `.env.example` listing every non-secret setting from Task 3, and CI running lint/type/test on `ubuntu-latest` (Docker is available there for testcontainers).
- [ ] **Step 5: Write `docs/decisions/README.md`.** It is an ADR index whose first entry links to the roadmap §1 (decisions C1–C10).
- [ ] **Step 6: Run `uv run pytest tests/unit/test_package.py -v`** — expect PASS. Run `make lint type` — expect clean.
- [ ] **Step 7: Commit** `chore: scaffold trading package, tooling and CI`.

---

### Task 2: Core primitives — Decimal, UTC, Clock, IDs, codes, hashing

**Files:**
- Create: `src/trading/core/{money.py,time.py,clock.py,ids.py,codes.py,enums.py,hashing.py,errors.py}`
- Test: `tests/unit/core/test_money.py`, `test_time.py`, `test_clock.py`, `test_codes.py`, `test_hashing.py`

**Interfaces:**
- Produces:
  - `money.to_decimal(value: str | int | Decimal) -> Decimal` raises `TypeError` for `float`
  - `money.quantize_price(v: Decimal) -> Decimal` rounds to 8 dp, HALF_EVEN
  - `money.quantize_cash(v: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal` rounds to 4 dp
  - `money.floor_to_increment(v: Decimal, inc: Decimal) -> Decimal`, `money.ceil_to_increment(v: Decimal, inc: Decimal) -> Decimal`, `money.is_multiple_of(v: Decimal, inc: Decimal) -> bool`
  - `money.trade_cash_amount(side: Side, qty: Decimal, price: Decimal) -> Decimal` returns the signed cash amount (BUY negative), via `quantize_cash`
  - `time.NaiveDatetimeError(ValueError)`; `time.ensure_utc(dt: datetime) -> datetime`, which converts aware datetimes to UTC; `time.UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]`; `time.isoformat_z(dt) -> str`
  - `clock.Clock` (Protocol: `now() -> datetime`), `clock.SystemClock`, `clock.ManualClock(start: datetime)` with `.advance(delta: timedelta)` and `.set(dt: datetime)`
  - `ids.new_id() -> UUID` (uuid4); `ids.make_client_order_id(strategy_key: str, session_date: date, seq: int) -> str`, giving `"paper-orb_v1-20261019-0001"`
  - `enums.Side(StrEnum)` BUY/SELL; `enums.OrderType` MARKET/LIMIT; `enums.TimeInForce` DAY; `enums.AssetType` EQUITY/ETF
  - `codes.RejectionCode(StrEnum)`, values equal to names, in this exact (evaluation) order: TRADING_PAUSED, RISK_STATE_UNKNOWN, RECONCILIATION_REQUIRED, DAILY_LOSS_LIMIT, DRAWDOWN_LIMIT, MARKET_CLOSED, SIGNAL_EXPIRED, STALE_MARKET_DATA, INVALID_QUOTE, STRATEGY_DISABLED, DUPLICATE_INTENT, UNSUPPORTED_ORDER, SHORT_NOT_ALLOWED, INVALID_TICK_SIZE, INVALID_QUANTITY, EXPOSURE_LIMIT, INSUFFICIENT_CASH, CASH_RESERVE_LIMIT, MAX_DAILY_ENTRIES, MAX_OPEN_SYMBOLS
  - `hashing.canonical_json(obj: object) -> str` (sorted keys, no whitespace; Decimal → `str`, normalized without exponent; datetime → `isoformat_z`; UUID/date/StrEnum → `str`); `hashing.content_hash(obj: object | None) -> str | None` (sha256 hex)
  - `errors.TradingError(Exception)` as the base of every domain error

- [ ] **Step 1: Write the failing tests** (key assertions)

```python
def test_to_decimal_rejects_float():
    with pytest.raises(TypeError): to_decimal(0.1)
def test_floor_to_increment_never_rounds_up():
    assert floor_to_increment(Decimal("100") / Decimal("576.00"), Decimal("0.0001")) == Decimal("0.1736")
    assert floor_to_increment(Decimal("0.99999999"), Decimal("1")) == Decimal("0")
def test_ceil_to_increment():
    assert ceil_to_increment(Decimal("100.030002"), Decimal("0.01")) == Decimal("100.04")
def test_trade_cash_amount_signs():
    assert trade_cash_amount(Side.BUY, Decimal("0.4"), Decimal("100.02")) == Decimal("-40.0080")
    assert trade_cash_amount(Side.SELL, Decimal("1"), Decimal("101")) == Decimal("101.0000")
def test_ensure_utc_rejects_naive():
    with pytest.raises(NaiveDatetimeError): ensure_utc(datetime(2026, 10, 19, 9, 30))
def test_ensure_utc_converts_new_york():
    ny = datetime(2026, 10, 19, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert ensure_utc(ny) == datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
def test_manual_clock_advance():
    c = ManualClock(datetime(2026, 10, 19, 13, 30, tzinfo=UTC)); c.advance(timedelta(minutes=1))
    assert c.now() == datetime(2026, 10, 19, 13, 31, tzinfo=UTC)
def test_manual_clock_rejects_naive():
    with pytest.raises(NaiveDatetimeError): ManualClock(datetime(2026, 10, 19))
def test_spec_codes_present():
    spec = {"MARKET_CLOSED","STALE_MARKET_DATA","INSUFFICIENT_CASH","EXPOSURE_LIMIT","DAILY_LOSS_LIMIT",
            "DRAWDOWN_LIMIT","DUPLICATE_INTENT","INVALID_TICK_SIZE","UNSUPPORTED_ORDER",
            "RECONCILIATION_REQUIRED","STRATEGY_DISABLED","SIGNAL_EXPIRED","RISK_STATE_UNKNOWN"}
    assert spec <= {c.value for c in RejectionCode}
def test_content_hash_stable_across_key_order_and_decimal_form():
    assert content_hash({"a": Decimal("1.50"), "b": 1}) == content_hash({"b": 1, "a": Decimal("1.5")})
def test_client_order_id_format():
    assert make_client_order_id("orb_v1", date(2026, 10, 19), 1) == "paper-orb_v1-20261019-0001"
```

- [ ] **Step 2: Run `uv run pytest tests/unit/core -v`** — expect FAIL (ImportError).
- [ ] **Step 3: Implement the modules** to the signatures above. `SystemClock.now()` is the only call to `datetime.now(UTC)` in the codebase.
- [ ] **Step 4: Run `uv run pytest tests/unit/core -v`** — expect PASS. Run `make type` — expect clean.
- [ ] **Step 5: Commit** `feat(core): decimal, utc, clock, ids, rejection codes, hashing`.

---

### Task 3: Configuration and the PAPER_ONLY guard

**Files:**
- Create: `src/trading/core/config.py`, `configs/system.yaml`, `configs/risk.yaml`, `configs/strategies.yaml`, `configs/paper_broker.yaml`
- Test: `tests/unit/core/test_config.py`

**Interfaces:**
- Consumes: `UtcDatetime`, `to_decimal` (Task 2)
- Produces (all Pydantic, `frozen=True`, `extra="forbid"`, Decimal fields parsed from strings):
  - `SystemConfig(mode: Literal["PAPER_ONLY"], timezone: Literal["America/New_York"], starting_cash_usd: Decimal, require_fresh_market_data: bool)`
  - `UniverseEntry(symbol: str, asset_type: AssetType, sector: str, tick_size: Decimal, lot_size: Decimal, fractional_eligible: bool)`
  - `RiskPolicy(max_gross_exposure_pct, max_symbol_exposure_pct, max_sector_exposure_pct, max_daily_loss_pct, max_drawdown_pct: Decimal; max_new_entries_per_day: int; max_open_symbols: int; min_cash_reserve_pct: Decimal; allow_margin, allow_short, allow_options, allow_crypto: Literal[False]; max_bar_age_seconds: int = 90; entry_cutoff_minutes_before_close: int = 15; max_spread_bps: Decimal; unknown_order_grace_seconds: int = 30)`
  - `PaperBrokerConfig(latency_ms: int, slippage_bps: Decimal, max_participation_rate: Decimal, quote_max_age_seconds: int, commission_per_order_usd: Decimal, sell_fee_rate: Decimal, taf_per_share_usd: Decimal, taf_max_per_trade_usd: Decimal, fractional_shares: bool, fractional_increment: Decimal, settlement_days: int, use_unsettled_funds: Literal[False], seed: int)`
  - `StrategyConfig(enabled: bool, allocation_pct: Decimal, shadow_only: bool = False)`
  - `AppConfig(system, universe: tuple[UniverseEntry, ...], risk: RiskPolicy, paper_broker: PaperBrokerConfig, strategies: Mapping[str, StrategyConfig])` with property `limits_version -> str` (the first 16 hex characters of `content_hash(risk.model_dump())`)
  - `load_config(config_dir: Path) -> AppConfig`. A validation failure raises `ConfigError(TradingError)` whose message names the file.
  - `Settings(BaseSettings)` with env prefix `TRADING_`: `config_dir: Path = Path("configs")`, `db_host`, `db_port: int = 5432`, `db_name`, `db_user`, `db_password_file: Path | None`, `database_url: str | None` (tests only), `log_level: str = "INFO"`, `log_json: bool = True`, `worker_id: str` (default hostname), `metrics_port: int | None`. Method `resolved_database_url() -> str` reads the password file. `read_secret(path: Path) -> str` strips a trailing newline.

- [ ] **Step 1: Write the YAML files.** Copy the spec's example config values verbatim, split per C10. Then:
  - `risk.yaml` adds `max_spread_bps: '25'`.
  - `paper_broker.yaml`: `latency_ms: 500`, `slippage_bps: '2'`, `max_participation_rate: '0.05'`, `quote_max_age_seconds: 5`, `commission_per_order_usd: '0.00'`, `sell_fee_rate: '0.0000278'`, `taf_per_share_usd: '0.000166'`, `taf_max_per_trade_usd: '8.30'`, `fractional_shares: true`, `fractional_increment: '0.0001'`, `settlement_days: 1`, `use_unsettled_funds: false`, `seed: 7`. Put the comment `# PLACEHOLDER fee rates — verify current SEC/FINRA values` on the fee lines.
  - `system.yaml` universe: SPY, QQQ, IWM (ETF, sector `Broad Market`), AAPL and MSFT (EQUITY, `Technology`). All with tick `0.01`, lot `1`, `fractional_eligible: true`.
- [ ] **Step 2: Write the failing tests**

```python
def test_loads_repo_configs():
    cfg = load_config(Path("configs"))
    assert cfg.system.mode == "PAPER_ONLY"
    assert cfg.system.starting_cash_usd == Decimal("1000.00")
    assert cfg.risk.max_symbol_exposure_pct == Decimal("0.10") and cfg.risk.max_new_entries_per_day == 10
    assert len(cfg.limits_version) == 16
def test_live_mode_rejected(tmp_config_dir):          # fixture copies configs/ to tmp_path, then edits one key
    edit(tmp_config_dir / "system.yaml", ["system", "mode"], "LIVE")
    with pytest.raises(ConfigError, match="system.yaml"): load_config(tmp_config_dir)
@pytest.mark.parametrize("flag", ["allow_margin", "allow_short", "allow_options", "allow_crypto"])
def test_forbidden_risk_flags_rejected(tmp_config_dir, flag): ...   # set true → ConfigError
def test_float_values_rejected(tmp_config_dir):       # starting_cash_usd: 1000.0 (YAML float) → ConfigError
def test_allocation_sum_over_one_rejected(tmp_config_dir):   # sum(allocation_pct) > 1.00 → ConfigError
def test_unknown_key_rejected(tmp_config_dir):        # risk.yaml gains disable_risk: true → ConfigError
def test_limits_version_changes_with_risk(tmp_config_dir): ...
```

- [ ] **Step 3: Run** — expect FAIL.
- [ ] **Step 4: Implement `config.py`.** Decimal fields use a `BeforeValidator` that rejects `float` (YAML `1000.0` loads as float, so numbers must be quoted). An `AppConfig` `model_validator` enforces that allocations sum to ≤ 1.
- [ ] **Step 5: Run** — expect PASS. **Commit** `feat(config): typed config with PAPER_ONLY and risk-flag guards`.

---

### Task 4: Structured logging with secret redaction

**Files:**
- Create: `src/trading/monitoring/logging.py`
- Test: `tests/unit/monitoring/test_logging.py`

**Interfaces:**
- Produces: `configure_logging(level: str = "INFO", json_output: bool = True) -> None`; `redact(logger: object, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]` (a structlog processor); `bind_request_id(request_id: str) -> None` and `clear_context() -> None` (via structlog contextvars); `get_logger(name: str) -> BoundLogger`.

- [ ] **Step 1: Write the failing tests.** Use `structlog.testing.capture_logs` plus a JSON render test via capsys.

```python
def test_redacts_sensitive_keys():
    out = redact(None, "info", {"event": "x", "password": "p", "api_key": "k", "db_password_file": "/run/secrets/x", "nested": {"token": "t"}})
    assert out["password"] == out["api_key"] == out["nested"]["token"] == "***"
    assert out["db_password_file"] == "/run/secrets/x"     # paths to secrets are fine, values are not
def test_redacts_bearer_in_strings():
    assert "trk_abc" not in redact(None, "info", {"event": "Authorization: Bearer trk_abc"})["event"]
def test_json_output_includes_request_id(capsys): ...   # bind_request_id("r1"); log; json.loads(line)["request_id"] == "r1"
```

- [ ] **Step 2: Run** (FAIL) → **implement**. Sensitive key regex: `(?i)(password|secret|token|api[_-]?key|authorization)$`; keys ending in `_file` are exempt. String patterns: `Bearer\s+\S+` and `trk_[A-Za-z0-9_-]+`. Timestamps use the ISO UTC `TimeStamper(fmt="iso", utc=True)`. → **Run** (PASS) → **Commit** `feat(monitoring): json logging with secret redaction`.

---

### Task 5: Database harness, Alembic, ops schema, roles and immutability

**Files:**
- Create: `src/trading/db/{base.py,engine.py,migrate.py,seed.py,models/ops.py}`, `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_ops.py`, `deploy/postgres/init-roles.sh`
- Test: `tests/conftest.py`, `tests/integration/db/test_schema_ops.py`, `tests/unit/db/test_no_float_columns.py`

**Interfaces:**
- Consumes: `Settings` (Task 3)
- Produces:
  - `db.base.Base(DeclarativeBase)` with `type_annotation_map = {Decimal: Numeric(20, 8), datetime: DateTime(timezone=True), dict[str, Any]: JSONB}`. Currency columns explicitly use `Numeric(20, 4)`.
  - `db.engine.make_engine(url: str) -> Engine` (`pool_pre_ping=True`); `make_session_factory(engine: Engine) -> sessionmaker[Session]` (`expire_on_commit=False`)
  - `db.migrate.upgrade(url: str, revision: str = "head") -> None`
  - `db.seed.seed_defaults(session: Session, now: datetime) -> None`, which inserts the `system_state` singleton if it is missing
  - ORM models in `db/models/ops.py`: `Principal`, `IdempotencyKey`, `JobRow`, `LeaseRow`, `OutboxRow`, `AuditEventRow`, `SystemStateRow`
  - Test fixtures in `tests/conftest.py`: `database_url` (session scope; uses `TEST_DATABASE_URL` if set, else starts `PostgresContainer("postgres:16-alpine")`), `session_factory` (session scope; runs `upgrade` once), `db` (function scope; yields a `Session`; at teardown it TRUNCATEs every table in schemas `public` and `paper_venue` except `alembic_version` with `RESTART IDENTITY CASCADE`, then calls `seed_defaults`), `clock` (`ManualClock` at `2026-10-19T13:30:00Z`)
- Migration `0001_ops` tables (in addition to the spec, per roadmap §1.2):
  - `principals(id uuid pk, name text unique, role text check in ('viewer','operator'), api_key_hash text unique, created_at, disabled_at null)`
  - `idempotency_keys(principal_id fk, route text, key text, request_hash text, status_code int, response_json jsonb, created_at, pk(principal_id,route,key))`
  - `jobs(id uuid pk, kind text, payload_json jsonb, state text check in ('QUEUED','RUNNING','SUCCEEDED','DEAD'), scheduled_at, lease_until null, locked_by null, attempts int default 0, max_attempts int default 5, last_error null, dedupe_key text unique, created_at, updated_at)` with index `(state, scheduled_at)`
  - `leases(name text pk, holder text, fencing_token bigint, acquired_at, expires_at)`
  - `outbox(id uuid pk, event_type text, payload_json jsonb, created_at, delivered_at null)` with a partial index `WHERE delivered_at IS NULL`
  - `audit_events(id uuid pk, actor text, action text, target text, ts, request_id text null, before_hash text null, after_hash text null, result text, details_json jsonb)` with index `(ts desc, id desc)`
  - `system_state(id smallint pk check (id=1), mode text check (mode='PAPER_ONLY'), trading_state text, risk_state text, risk_reason text null, version int, updated_at, last_reconciled_at null)`, seeded `('PAPER_ONLY','PAUSED','UNKNOWN', version 1)`
  - Function `forbid_mutation()` that raises `'% is append-only', TG_TABLE_NAME`, attached as a `BEFORE UPDATE OR DELETE` row trigger on `audit_events`
  - Final step: `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO trading_app` plus `ALTER DEFAULT PRIVILEGES`, wrapped in `DO $$ IF EXISTS (role trading_app) $$` so the tests' superuser DB works too
- `deploy/postgres/init-roles.sh` creates the roles `trading_owner` (owns DB `trading`) and `trading_app` (LOGIN, no TRUNCATE) from `/run/secrets/db_owner_password` and `/run/secrets/db_app_password`.

- [ ] **Step 1: Write the failing tests**

```python
def test_migration_creates_ops_tables(db): ...   # inspect(db.bind).get_table_names() ⊇ {"jobs","leases","outbox","audit_events","system_state","principals","idempotency_keys"}
def test_system_state_seeded(db):
    row = db.execute(text("select mode, trading_state, risk_state, version from system_state")).one()
    assert tuple(row) == ("PAPER_ONLY", "PAUSED", "UNKNOWN", 1)
def test_system_state_rejects_live_mode(db):
    with pytest.raises(IntegrityError): db.execute(text("update system_state set mode='LIVE'")); db.flush()
def test_audit_events_append_only(db):     # insert one row, then UPDATE → raises DBAPIError matching "append-only"; DELETE → same
def test_no_float_columns():               # unit test: iterate Base.metadata, assert no column type is Float/REAL/DOUBLE
```

- [ ] **Step 2: Run `uv run pytest tests/integration/db tests/unit/db -v`** — expect FAIL.
- [ ] **Step 3: Implement the base, engine, migrate, seed, ops models, Alembic env** (it reads the URL from the `-x url=` argument or `Settings`) **and migration 0001** with hand-written DDL for the trigger, the CHECKs, and the grants.
- [ ] **Step 4: Run** — expect PASS. Run `uv run alembic downgrade base && uv run alembic upgrade head` against the test container URL to check that downgrade works.
- [ ] **Step 5: Commit** `feat(db): postgres harness, alembic, ops schema, append-only audit`.

---

### Task 6: Durable job queue and runner

**Files:**
- Create: `src/trading/jobs/queue.py`, `src/trading/jobs/runner.py`
- Test: `tests/integration/jobs/test_queue.py`, `tests/integration/jobs/test_runner.py`

**Interfaces:**
- Consumes: `JobRow`, `session_factory`, `Clock`
- Produces:
  - `JobState(StrEnum)` QUEUED/RUNNING/SUCCEEDED/DEAD; `Job` (frozen dataclass: `id: UUID, kind: str, payload: Mapping[str, Any], attempts: int, max_attempts: int`)
  - `enqueue(session, *, kind: str, payload: Mapping[str, Any], scheduled_at: datetime, dedupe_key: str, max_attempts: int = 5) -> UUID`. Uses `INSERT … ON CONFLICT (dedupe_key) DO NOTHING`, then returns the existing id.
  - `claim(session, *, worker_id: str, now: datetime, lease_for: timedelta, kinds: Collection[str] | None = None) -> Job | None`. Selects `state='QUEUED' AND scheduled_at<=now`, or `state='RUNNING' AND lease_until<now`, ordered by `scheduled_at`, `FOR UPDATE SKIP LOCKED LIMIT 1`. Sets RUNNING, `lease_until`, `locked_by`, and `attempts+1`.
  - `complete(session, job_id: UUID, *, now: datetime) -> None`
  - `fail(session, job_id: UUID, *, error: str, now: datetime) -> JobState`. Returns QUEUED with `scheduled_at = now + min(2**attempts, 300)s`, or DEAD once `attempts >= max_attempts`. `last_error` is truncated to 2000 chars.
  - `runner.Handler = Callable[[Job], None]`; `JobRunner(session_factory, handlers: Mapping[str, Handler], *, clock: Clock, worker_id: str, lease_for: timedelta = timedelta(minutes=5))` with `.run_once() -> bool` (claim → handler → complete/fail, each in its own transaction; an unknown kind → `fail(error="no handler for kind")`). Returns False when nothing was claimed.

- [ ] **Step 1: Write the failing tests**

```python
def test_enqueue_dedupes(db, clock): a = enqueue(..., dedupe_key="k"); b = enqueue(..., dedupe_key="k"); assert a == b and count_jobs(db) == 1
def test_not_claimed_before_scheduled(db, clock): ...
def test_concurrent_claims_get_distinct_jobs(session_factory, clock):
    # enqueue 2 jobs; open two sessions; claim in s1 without committing; claim in s2 → returns the other job; a third claim in s2 → None
def test_expired_lease_is_reclaimed(...): ...      # claim with lease 1 min; advance clock 2 min; claim by another worker → same job, attempts == 2
def test_fail_backoff_then_dead(...): ...          # max_attempts=2: first fail → QUEUED at now+2s; second → DEAD
def test_runner_unknown_kind_fails(...): ...
def test_runner_handler_exception_records_error(...): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(jobs): durable queue with skip-locked claims and backoff`.

---

### Task 7: Leases with fencing tokens

**Files:**
- Create: `src/trading/jobs/lease.py`
- Test: `tests/integration/jobs/test_lease.py`

**Interfaces:**
- Produces: `EXECUTOR_LEASE = "executor"`, `SCHEDULER_LEASE = "scheduler"`; `Lease` (frozen: `name, holder, fencing_token: int, expires_at: datetime`); `LeaseLost(TradingError)`.
  - `acquire_lease(session, *, name, holder, now, ttl: timedelta) -> Lease | None`. Inserts if absent. Takes over if expired, or if held by the same holder. Every takeover by a *different* holder increments `fencing_token`.
  - `renew_lease(session, lease, *, now, ttl) -> Lease | None`. Returns None if the row's holder or token differs from `lease`, or if the lease has already expired.
  - `release_lease(session, lease) -> None` (sets `expires_at = acquired_at`, only when holder and token match)
  - `current_lease(session, name, *, now) -> Lease | None`
  - `assert_fence(session, lease, *, now) -> None`. Does `SELECT … FOR SHARE` and raises `LeaseLost` unless holder and token match and `expires_at > now`. **Every write path in the worker calls this inside its transaction.**

- [ ] **Step 1: Write the failing tests**

```python
def test_second_holder_blocked_while_valid(...): assert acquire_lease(..., holder="w2") is None
def test_takeover_after_expiry_increments_token(...): l1 = acquire(w1); advance(ttl+1s); l2 = acquire(w2); assert l2.fencing_token == l1.fencing_token + 1
def test_stale_holder_renew_fails(...): ...       # after the takeover, renew_lease(l1) is None
def test_stale_holder_fence_fails(...): ...       # assert_fence(l1) raises LeaseLost
def test_release_allows_immediate_acquire(...): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(jobs): fenced leases for executor and scheduler`.

---

### Task 8: Audit log and transactional outbox

**Files:**
- Create: `src/trading/monitoring/audit.py`, `src/trading/jobs/outbox.py`
- Test: `tests/integration/test_audit_outbox.py`

**Interfaces:**
- Consumes: `content_hash` (Task 2), `AuditEventRow`, `OutboxRow`
- Produces:
  - `record_audit(session, *, actor: str, action: str, target: str, request_id: str | None, before: object | None, after: object | None, result: str, now: datetime, details: Mapping[str, Any] | None = None) -> UUID`. Stores `content_hash(before)` / `content_hash(after)`, never the raw objects (details must not carry secrets).
  - `list_audit(session, *, limit: int, before: tuple[datetime, UUID] | None) -> list[AuditEventRow]` (keyset, newest first)
  - `emit_event(session, *, event_type: str, payload: Mapping[str, Any], now: datetime) -> UUID`. The payload is passed through `canonical_json` first, so Decimals and datetimes serialize safely.
  - `OutboxHandler = Callable[[str, Mapping[str, Any]], None]`; `dispatch_pending(session, handlers: Mapping[str, OutboxHandler], *, now, limit: int = 100) -> int`. Uses `FOR UPDATE SKIP LOCKED` and falls back to `log_event` (a structured warning log) for event types with no handler. A handler exception leaves that row undelivered and continues with the next.

- [ ] **Step 1: Write the failing tests**

```python
def test_audit_hashes_not_raw(db, clock): eid = record_audit(..., before={"state": "PAUSED"}, after={"state": "PAPER_RUNNING"}, ...); row = ...; assert row.before_hash == content_hash({"state": "PAUSED"})
def test_outbox_rolls_back_with_business_txn(session_factory, clock):
    with pytest.raises(RuntimeError):
        with session_factory.begin() as s: emit_event(s, ...); raise RuntimeError
    assert outbox_count() == 0
def test_dispatch_marks_delivered_and_skips_failed_handler(...): ...
def test_list_audit_keyset_pagination(...): ...   # 5 events, limit 2 → 3 pages, no duplicates, newest first
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat: immutable audit log and transactional outbox`.

---

### Task 9: System state (trading and risk state, versioned)

**Files:**
- Create: `src/trading/control/state.py`
- Test: `tests/integration/control/test_state.py`

**Interfaces:**
- Consumes: `record_audit`, `emit_event`
- Produces:
  - `TradingState(StrEnum)` PAUSED/PAPER_RUNNING; `RiskState(StrEnum)` NORMAL/HALTED_DAILY_LOSS/HALTED_DRAWDOWN/RECONCILIATION_REQUIRED/UNKNOWN
  - `SystemState` (frozen: `mode: Literal["PAPER_ONLY"], trading_state, risk_state, risk_reason: str | None, version: int, updated_at, last_reconciled_at: datetime | None`), with property `trading_enabled = trading_state is PAPER_RUNNING and risk_state is NORMAL`
  - `VersionConflict(TradingError)` with attribute `current_version: int`
  - `get_system_state(session, *, for_update: bool = False) -> SystemState`
  - `set_trading_state(session, *, target: TradingState, expected_version: int, actor: str, reason: str, request_id: str | None, now) -> tuple[SystemState, UUID]`. Raises `VersionConflict` if the version differs. If the state is already `target`: no version bump, audit `result="noop"`. Otherwise: bump the version, audit `result="ok"`, and emit outbox `trading_state_changed`. Returns `(state, audit_event_id)`.
  - `set_risk_state(session, *, risk_state: RiskState, reason: str, actor: str, now) -> SystemState`. Bumps the version, audits, and emits `risk_state_changed` only on an actual change.
  - `mark_reconciled(session, *, now) -> None`

- [ ] **Step 1: Write the failing tests:** `test_pause_bumps_version_and_audits`, `test_stale_expected_version_conflicts` (asserts `exc.current_version`), `test_same_state_is_noop_without_version_bump`, `test_risk_state_change_emits_outbox_event`, `test_trading_enabled_requires_normal_risk`.
- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(control): versioned trading and risk state`.

---

### Task 10: API skeleton — request IDs, Problem Details, health, metrics

**Files:**
- Create: `src/trading/api/{app.py,deps.py,problems.py,middleware.py,pagination.py,routes/health.py}`, `src/trading/monitoring/metrics.py`
- Test: `tests/integration/api/test_health.py`, `tests/unit/api/test_problems.py`, `tests/unit/api/test_pagination.py`

**Interfaces:**
- Consumes: `get_system_state`, `current_lease`, `EXECUTOR_LEASE`, `Clock`, `Settings`, `AppConfig`
- Produces:
  - `create_app(*, settings: Settings, config: AppConfig, session_factory: sessionmaker[Session], clock: Clock) -> FastAPI` (dependencies stored on `app.state`; Task 26 adds a keyword `calendar: MarketCalendar | None = None`)
  - `deps.get_session` (yields a session inside `begin()`), `deps.get_clock`, `deps.get_config`
  - `ApiProblem(Exception)(status: int, code: str, title: str, detail: str | None = None, extra: Mapping[str, Any] | None = None)`. Responses use `application/problem+json`: `{type: f"urn:trading:problem:{code}", title, status, detail, instance: request.url.path, code, request_id, **extra}`. Handlers exist for `ApiProblem`, `RequestValidationError` (422 `VALIDATION_ERROR` with `errors`), `VersionConflict` (409 `VERSION_CONFLICT` with `current_version`), DB `OperationalError` (503 `DATABASE_UNAVAILABLE`), and any other exception (500 `INTERNAL`, no stack trace in the body).
  - `RequestIdMiddleware`. Accepts an incoming `X-Request-ID` matching `^[A-Za-z0-9._-]{1,128}$`, else generates a uuid4. Echoes it on the response and binds it to the log context.
  - `pagination.encode_cursor(ts: datetime, id: UUID) -> str` (base64url JSON) / `decode_cursor(s: str) -> tuple[datetime, UUID]`. A bad cursor raises `ApiProblem(422, "INVALID_CURSOR", …)`.
  - `GET /v1/health/live` → `{"status":"ok"}`, with no auth and no DB access
  - `GET /v1/health/ready` → `{status: "ready"|"not_ready", database: "ok"|"error", market_data: "ok"|"stale"|"closed"|"not_configured", executor_lease: "held"|"missing"}`. HTTP 200 if `database=="ok" and executor_lease=="held"`, else 503. No auth: it exposes no sensitive data, and the Compose healthcheck uses it. `market_data` is the constant `"not_configured"` until Task 26.
  - `compute_readiness(session_factory, clock) -> Readiness`, reused by resume-paper in Task 11
  - `metrics.py`: `HTTP_REQUESTS = Counter("trading_http_requests_total", …, ["method","route","status"])`, `JOBS_PROCESSED`, `LEASE_HELD = Gauge("trading_lease_held", …, ["name"])`, `ORDERS_BY_STATUS = Counter(…, ["status"])`. **No symbol, account or strategy labels.** `start_metrics_server(port: int | None) -> None` (no-op when None).

- [ ] **Step 1: Write the failing tests**

```python
def test_live_ok(client): assert client.get("/v1/health/live").json() == {"status": "ok"}
def test_ready_503_without_executor_lease(client): r = client.get("/v1/health/ready"); assert r.status_code == 503 and r.json()["executor_lease"] == "missing"
def test_ready_200_with_lease(client, db, clock): acquire_lease(... EXECUTOR_LEASE ...); assert client.get("/v1/health/ready").status_code == 200
def test_ready_503_when_db_down(): app = create_app(..., session_factory=make_session_factory(make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")), ...); r = TestClient(app).get("/v1/health/ready"); assert r.status_code == 503 and r.json()["database"] == "error"
def test_request_id_echoed_or_generated(client): ...   # valid id echoed; "bad id!" replaced by a uuid
def test_problem_shape(): ...                          # ApiProblem(409, "VERSION_CONFLICT", "x") → content-type problem+json, required keys
def test_cursor_roundtrip_and_invalid(): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(api): app factory, request ids, problem details, health`.

---

### Task 11: Authentication, idempotency, control and audit endpoints

**Files:**
- Create: `src/trading/api/{auth.py,idempotency.py,routes/system.py,routes/control.py,routes/audit.py}`; modify `src/trading/cli.py` (the `admin` sub-app)
- Test: `tests/integration/api/test_auth.py`, `test_control.py`, `test_audit_api.py`, `tests/integration/test_cli_admin.py`

**Interfaces:**
- Consumes: `Principal` model, `set_trading_state`, `compute_readiness`, `list_audit`, `encode_cursor`
- Produces:
  - `Role(StrEnum)` viewer/operator; `AuthPrincipal` (frozen: `id: UUID, name: str, role: Role`)
  - `generate_api_key() -> str` (`"trk_" + secrets.token_urlsafe(32)`); `hash_api_key(key: str) -> str` (sha256 hex; adequate because keys are 256-bit random)
  - `create_principal(session, *, name: str, role: Role, now) -> tuple[AuthPrincipal, str]` (the plaintext key is returned once, never stored)
  - Dependencies: `authenticate` (401 `UNAUTHENTICATED` + `WWW-Authenticate: Bearer`; disabled principals are rejected); `require_viewer` (any role); `require_operator` (403 `FORBIDDEN` otherwise)
  - `run_idempotent(session, *, principal: AuthPrincipal, route: str, key: str | None, body: BaseModel, action: Callable[[], tuple[int, dict[str, Any]]]) -> JSONResponse`:
    - missing key → 400 `IDEMPOTENCY_KEY_REQUIRED`
    - key longer than 128 → 422
    - existing row with a different `content_hash(body)` → 422 `IDEMPOTENCY_KEY_REUSED`
    - existing row with the same hash → replay the stored status/body with header `Idempotent-Replayed: true`
    - otherwise run `action` and insert the key row **in the same transaction**. A unique violation from a concurrent duplicate rolls everything back → 409 `IDEMPOTENCY_IN_PROGRESS`.
  - `POST /v1/control/pause` body `PauseRequest(reason: str 1..500, expected_version: int)` → 200 `{state: "PAUSED", event_id, version}`. Operator only. Always allowed (no readiness gate).
  - `POST /v1/control/resume-paper`, same body → 200 `{state: "PAPER_RUNNING", event_id, version}` only if readiness is `ready` **and** `risk_state == NORMAL` **and** `market_data != "stale"`. Otherwise 503 `NOT_READY` with `extra={"checks": {...}}`.
  - `GET /v1/system/status` (viewer) → `{mode, trading_enabled, trading_state, risk_state, risk_reason, version, session: null, last_data_at: null, last_reconciled_at}` (session/last_data fields are filled in Task 26)
  - `GET /v1/audit?cursor=&limit=` (viewer, limit 1..200, default 50) → `{items: [{id, actor, action, target, ts, request_id, before_hash, after_hash, result}], next_cursor}`
  - CLI: `trading admin create-api-key --name NAME --role {viewer,operator}` prints the key once on stdout. Any other output goes to stderr.

- [ ] **Step 1: Write the failing tests**

```python
def test_missing_token_401(client): r = client.get("/v1/system/status"); assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
def test_viewer_cannot_pause(client, viewer_key): assert post_pause(client, viewer_key).status_code == 403
def test_pause_requires_idempotency_key(client, operator_key): ...           # 400 IDEMPOTENCY_KEY_REQUIRED
def test_pause_is_idempotent(client, operator_key, db):
    r1 = post_pause(..., key="k1", expected_version=1); r2 = post_pause(..., key="k1", expected_version=1)
    assert r1.json() == r2.json() and r2.headers["idempotent-replayed"] == "true"
    assert count_audit(db, action="trading_state.set") == 1
def test_idempotency_key_reuse_with_different_body_422(...): ...
def test_stale_version_409(...): ...      # expected_version=99 → 409 VERSION_CONFLICT with current_version
def test_resume_refused_when_risk_unknown(...): ...   # lease held, risk UNKNOWN → 503 NOT_READY, checks.risk_state == "UNKNOWN"
def test_resume_succeeds_when_ready(...): ...         # lease held + set_risk_state(NORMAL) → 200 PAPER_RUNNING; status shows trading_enabled true
def test_control_actions_audited_with_request_id(...): ...
def test_audit_list_paginates(...): ...
def test_cli_create_api_key_prints_key_once(...): ...  # stdout matches ^trk_; DB stores the hash only
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(api): api-key auth, idempotent audited pause/resume, status, audit`.

---

### Task 12: Service entrypoints and Docker Compose (acceptance test 1)

**Files:**
- Create: `src/trading/services/{worker.py,scheduler.py,handlers.py}`, `src/trading/jobs/schedule.py`, `Dockerfile`, `docker-compose.yml`, `deploy/prometheus/prometheus.yml`, `scripts/init-secrets.sh`, `docs/runbooks/tailscale-ingress.md`, `docs/runbooks/startup-shutdown.md`; modify `cli.py` (`api`, `worker`, `scheduler`, `migrate`, `healthcheck`)
- Test: `tests/integration/services/test_worker_loop.py`, `tests/integration/services/test_scheduler_loop.py`, `tests/unit/jobs/test_schedule.py`, `tests/smoke/test_compose.py`

**Interfaces:**
- Consumes: `JobRunner`, leases, `dispatch_pending`, `enqueue`, `start_metrics_server`
- Produces:
  - `JobSpec` (frozen: `kind: str, dedupe_key: str, scheduled_at: datetime, payload: Mapping[str, Any]`); `plan_jobs(now: datetime, calendar: MarketCalendar | None) -> list[JobSpec]`. In Sprint 1 this is only `heartbeat` for the current minute (dedupe `heartbeat:2026-10-19T13:31Z`). Task 25 extends it.
  - `WorkerContext` (dataclass: `settings, config, session_factory, clock, lease: Lease`; Task 25 adds broker/manager fields); `build_handlers(ctx: WorkerContext) -> dict[str, Handler]` (Sprint 1: `heartbeat` → sets the `LEASE_HELD` gauge and logs)
  - `run_worker(ctx_factory, *, stop: threading.Event, lease_ttl: timedelta = 30s, poll: float = 1.0) -> int`. Loop: acquire `EXECUTOR_LEASE` (retry every 5 s until `stop`) → `startup_hooks` (empty list now; reconciliation is added in Task 25) → repeat {renew the lease every `ttl/3`; `runner.run_once()`; `dispatch_pending`}. **If a renew returns None or raises a DB error → log critical, return exit code 3.** On `stop` → release the lease and return 0.
  - `run_scheduler(..., stop) -> int`: acquire `SCHEDULER_LEASE`; every 10 s run `plan_jobs` and `enqueue` each spec; the same lease-loss rule (exit 3).
  - CLI: `trading api` (uvicorn on `0.0.0.0:8000`, plus `start_metrics_server(9100)`), `trading worker` (metrics 9101), `trading scheduler` (9102), `trading migrate` (`upgrade` with the owner credentials), `trading healthcheck {worker|scheduler}` (exit 0 iff `current_lease(name)` exists and its holder == `settings.worker_id`). SIGTERM/SIGINT set `stop`.
  - `Dockerfile`: multi-stage `python:3.12-slim`, `uv sync --frozen --no-dev`, non-root uid 10001, `ENTRYPOINT ["trading"]`.
  - `docker-compose.yml` services:
    - `postgres` (postgres:16, volume `pgdata`, secrets `db_owner_password`/`db_app_password`, init script mounted, healthcheck `pg_isready`)
    - `migrate` (`command: migrate`, user `trading_owner`, `depends_on postgres: service_healthy`)
    - `api` (`127.0.0.1:8000:8000`, `depends_on migrate: service_completed_successfully`, healthcheck via `python -c` urllib on `/v1/health/live`)
    - `worker`, `scheduler` (healthcheck `trading healthcheck …`; `restart: unless-stopped`)
    - `prometheus` (prom/prometheus, `127.0.0.1:9090:9090`, scraping `api:9100`, `worker:9101`, `scheduler:9102`)
    - All app services use DB user `trading_app`, except `migrate`. **No broker env vars or secrets exist anywhere.** `secrets:` map to files under `./secrets/`, which `scripts/init-secrets.sh` creates (random 32-byte values, `chmod 600`, never overwritten).
  - Runbooks. Tailscale: `tailscale serve --bg --https=443 http://127.0.0.1:8000`; never `funnel`. Startup/shutdown follow the spec's Operational runbook, mapped to the concrete commands.

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_jobs_heartbeat_dedupe_per_minute(): a = plan_jobs(t("13:31:05"), None); b = plan_jobs(t("13:31:55"), None); assert a[0].dedupe_key == b[0].dedupe_key
def test_worker_processes_job_and_releases_lease_on_stop(...): ...  # worker in a thread; enqueue heartbeat; wait for SUCCEEDED; set stop; lease released
def test_worker_exits_when_lease_renew_fails(...): ...   # steal the lease (new holder, token+1) → run_worker returns 3 within 2 renew periods
def test_only_one_worker_holds_executor_lease(...): ...  # two workers; exactly one LEASE holder; the other never runs a job
def test_scheduler_enqueues_once_per_minute(...): ...
```

```python
@pytest.mark.smoke
def test_compose_up_healthy():
    # subprocess: scripts/init-secrets.sh; docker compose up -d --build; poll GET http://127.0.0.1:8000/v1/health/ready until 200 (timeout 180 s)
    # assert body == {"status":"ready","database":"ok","market_data":"not_configured","executor_lease":"held"}
    # assert "BROKER" not in `docker compose config` output; finally: docker compose down -v
```

- [ ] **Step 2: Run** (FAIL) → **implement** → `uv run pytest tests/integration/services tests/unit/jobs -v` (PASS) → `make smoke` (PASS).
- [ ] **Step 3: Commit** `feat(services): worker/scheduler with fenced leases, dockerized stack, runbooks`.

**Sprint 1 milestone:** `make lint type test smoke` is all green. Report progress before starting Sprint 2.

---

# Sprint 2 — Data, Paper Execution, Risk

### Task 13: Trading schema, ORM models, instruments

**Files:**
- Create: `migrations/versions/0002_trading.py`, `src/trading/db/models/{market.py,trading.py,portfolio.py,research.py,venue.py}`, `src/trading/data/instruments.py`
- Test: `tests/integration/db/test_schema_trading.py`, `tests/integration/data/test_instruments.py`

**Interfaces:**
- Produces: ORM classes `Account, Instrument, MarketBar, MarketQuote, MarketSnapshotRow, CorporateAction, Strategy, Signal, AllocationDecision, AgentRun, RiskDecisionRow, OrderIntent, Order, OrderEvent, FillRow, CashLedgerEntry, Position, StrategyAttribution, EquitySnapshot, TradingSession, ReconciliationRun, VenueAccount, VenueOrder, VenueFill`. Columns are the spec's data model plus roadmap §1.2. The non-obvious ones:
  - `accounts(id, name unique, broker, mode check ='PAPER', currency, status, peak_equity numeric(20,4), created_at)`
  - `instruments(id, symbol, venue, asset_type check in ('EQUITY','ETF'), tick_size, lot_size, fractional_eligible, sector, active, unique(symbol, venue))`
  - `market_bars(... spec ..., session_date date, source_seq text null, quality_flags text[] default '{}')`, PK `(instrument_id, interval, ts, provider)`, index `(instrument_id, ts)`
  - `market_quotes(instrument_id, ts, bid, ask, bid_size, ask_size, provider, ingested_at, quality_flags, pk(instrument_id, ts, provider))`
  - `market_snapshots(id, as_of, provider, data_hash, quality_status check in ('OK','DEGRADED','STALE'), content_json)`
  - `corporate_actions(id, instrument_id, action_type check in ('SPLIT','CASH_DIVIDEND'), ex_date, ratio_from, ratio_to, cash_amount, provider, announced_at, ingested_at, unique(instrument_id, action_type, ex_date, provider))`
  - `risk_decisions(id, signal_id null fk, account_id, instrument_id, ts, allowed, reasons_json, limits_version, market_snapshot_id fk not null, portfolio_snapshot_id fk equity_snapshots null, dedupe_key, proposal_json)`. A partial unique index on `dedupe_key WHERE allowed`.
  - `order_intents(id, account_id, instrument_id, side, quantity, order_type, limit_price, time_in_force, strategy_ids text[], market_snapshot_id fk NOT NULL, risk_decision_id fk NOT NULL UNIQUE, client_order_id UNIQUE, expires_at null, is_entry bool, created_at)`
  - `orders(id, account_id, intent_id fk unique not null, instrument_id, client_order_id unique, broker_order_id null, side, type, time_in_force, limit_price, qty, filled_qty default 0, avg_fill_price null, status, status_reason null, version, created_at, updated_at)`, with CHECKs `filled_qty >= 0 AND filled_qty <= qty AND qty > 0`, and indexes `(status)` and `(created_at)`
  - `order_events(id, order_id, ts, from_status, to_status, reason, payload_json)`
  - `fills(id, order_id, execution_id unique, ts, qty check >0, price check >0, fees, slippage_estimate)`
  - `cash_ledger(id, account_id, ts, amount numeric(20,4), currency, source_type check in ('INITIAL_DEPOSIT','TRADE','FEE','DIVIDEND','REVERSAL','ADJUSTMENT'), source_id, settles_on date, reverses_id null fk)`
  - `positions(account_id, instrument_id, qty check >= 0, average_cost, realized_pnl numeric(20,4), updated_at, pk(account_id, instrument_id))`
  - `equity_snapshots(id, account_id, ts, kind check in ('INTRADAY','SESSION_START','SESSION_END'), cash, settled_cash, equity, exposure, realized_pnl, unrealized_pnl, positions_json)`
  - `trading_sessions(account_id, session_date, start_equity, new_entries int default 0, daily_loss_halted_at null, pk(account_id, session_date))`
  - `reconciliation_runs(id, account_id, started_at, completed_at, status check in ('OK','PENDING','MISMATCH','ERROR'), report_json)`
  - schema `paper_venue`: `venue_accounts(account_ref pk, starting_cash)`, `venue_orders(client_order_id pk, broker_order_id unique, account_ref, symbol, side, type, tif, limit_price, qty, filled_qty, status, submitted_at, eligible_at, fill_seq int, updated_at)`, `venue_fills(execution_id pk, client_order_id, account_ref, symbol, side, ts, qty, price, fees, slippage_estimate)`
  - The `forbid_mutation` trigger on `fills, cash_ledger, order_intents, order_events, risk_decisions, paper_venue.venue_fills`. Grants as in 0001, including `paper_venue`.
  - `data.instruments.InstrumentInfo` (frozen: `id: UUID, symbol, asset_type: AssetType, tick_size, lot_size, fractional_eligible: bool, sector: str`); `upsert_universe(session, universe: Sequence[UniverseEntry]) -> dict[str, InstrumentInfo]`; `load_instruments(session) -> dict[str, InstrumentInfo]`; `qty_increment(instrument: InstrumentInfo, cfg: PaperBrokerConfig) -> Decimal` (= `cfg.fractional_increment` if both `cfg.fractional_shares` and `instrument.fractional_eligible`, else `instrument.lot_size`)

- [ ] **Step 1: Write the failing tests:** `test_trading_tables_exist`; `test_order_intent_requires_risk_decision_and_snapshot` (NULL → IntegrityError); `test_filled_qty_cannot_exceed_qty`; `test_negative_position_rejected`; `test_account_mode_live_rejected`; `test_asset_type_option_rejected`; `test_fills_and_ledger_append_only`; `test_execution_id_unique`; `test_no_float_columns` (already present; now covers the new models); `test_upsert_universe_idempotent`; `test_qty_increment_whole_vs_fractional`.
- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(db): trading, portfolio and paper-venue schema`.

---

### Task 14: Exchange calendar (XNYS)

**Files:**
- Create: `src/trading/data/calendar.py`
- Test: `tests/unit/data/test_calendar.py`

**Interfaces:**
- Produces: `SessionInfo` (frozen: `session_date: date, open_at: datetime, close_at: datetime, is_half_day: bool`; times are UTC); `CalendarUnavailable(TradingError)`; Protocol `MarketCalendar` with `session_on(d: date) -> SessionInfo | None`, `session_at(ts: datetime) -> SessionInfo | None` (only if `open_at <= ts < close_at`), `next_session_after(d: date) -> SessionInfo`, `add_sessions(d: date, n: int) -> date` (for settlement); `XnysCalendar(start: date, end: date)` built on `exchange_calendars.get_calendar("XNYS", start, end)`. Dates outside `[start, end]` raise `CalendarUnavailable`.

- [ ] **Step 1: Write the failing tests**

```python
cal = XnysCalendar(date(2026, 1, 1), date(2027, 12, 31))
def test_regular_day_edt(): s = cal.session_on(date(2026, 10, 19)); assert (s.open_at, s.close_at) == (utc("2026-10-19T13:30"), utc("2026-10-19T20:00")) and not s.is_half_day
def test_after_dst_end_est(): assert cal.session_on(date(2026, 11, 2)).open_at == utc("2026-11-02T14:30")
def test_thanksgiving_closed(): assert cal.session_on(date(2026, 11, 26)) is None
def test_black_friday_half_day(): s = cal.session_on(date(2026, 11, 27)); assert s.close_at == utc("2026-11-27T18:00") and s.is_half_day
def test_close_is_exclusive(): assert cal.session_at(utc("2026-10-19T20:00")) is None and cal.session_at(utc("2026-10-19T19:59:59")) is not None
def test_weekend_none(): assert cal.session_on(date(2026, 10, 24)) is None
def test_settlement_t_plus_1_skips_weekend(): assert cal.add_sessions(date(2026, 10, 23), 1) == date(2026, 10, 26)
def test_out_of_range_fails_closed(): with pytest.raises(CalendarUnavailable): cal.session_on(date(2030, 1, 2))
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(data): XNYS calendar with half-days and DST`.

---

### Task 15: Market data provider, CSV replay provider, ingestion with quality flags

**Files:**
- Create: `src/trading/data/{models.py,provider.py,replay_provider.py,ingest.py}`, `scripts/make_synthetic_fixtures.py`, `tests/fixtures/replay/2026-10-19/{bars.csv,quotes.csv,README.md}`, `tests/fixtures/replay/gaps/{bars.csv,quotes.csv}`; modify `cli.py` (`trading ingest-csv --path DIR --start ISO --end ISO`)
- Test: `tests/unit/data/test_replay_provider.py`, `tests/integration/data/test_ingest.py`

**Interfaces:**
- Consumes: `XnysCalendar`, `InstrumentInfo`/`load_instruments`, `UtcDatetime`, `Clock`
- Produces:
  - `Bar` (frozen Pydantic: `symbol: str, interval: Literal["1m"], ts: UtcDatetime` (bar *start*), `open, high, low, close: Decimal, volume: Decimal, provider: str, source_seq: str | None`), with property `available_at = ts + 1 min`
  - `Quote` (frozen: `symbol, ts: UtcDatetime, bid, ask: Decimal, bid_size, ask_size: Decimal | None, provider`), with properties `mid` and `spread_bps`
  - `CorporateActionRecord` (frozen: `symbol, action_type: Literal["SPLIT","CASH_DIVIDEND"], ex_date: date, ratio_from: Decimal | None, ratio_to: Decimal | None, cash_amount: Decimal | None, provider, announced_at: UtcDatetime`)
  - Protocol `MarketDataProvider`: `name: str`; `fetch_bars(symbols: Sequence[str], start: datetime, end: datetime) -> Iterable[Bar]` (bars with `start <= ts < end`); `fetch_quotes(...) -> Iterable[Quote]`; `fetch_corporate_actions(symbols, start: date, end: date) -> Iterable[CorporateActionRecord]`
  - `CsvReplayProvider(root: Path, name: str = "replay")`. Columns: bars `symbol,ts,open,high,low,close,volume,source_seq`; quotes `symbol,ts,bid,ask,bid_size,ask_size`; optional `corporate_actions.csv`. Timestamps must be ISO-8601 with an offset or `Z`; otherwise it raises `DataFormatError(TradingError)` naming the file and line. Lines starting with `#` are comments.
  - `QualityFlag(StrEnum)`: GAP_BEFORE, OUT_OF_ORDER, NON_POSITIVE_PRICE, OHLC_INCONSISTENT, OUTSIDE_SESSION, ZERO_VOLUME, DUPLICATE_CONFLICT. `BLOCKING_FLAGS = {NON_POSITIVE_PRICE, OHLC_INCONSISTENT, OUTSIDE_SESSION, DUPLICATE_CONFLICT}`.
  - `IngestReport` (dataclass: `inserted: int, duplicates: int, flag_counts: dict[QualityFlag, int], gaps: list[tuple[str, datetime, datetime]], unknown_symbols: set[str]`)
  - `ingest_bars(session, provider, *, symbols, start, end, calendar, clock) -> IngestReport`. Sets `ingested_at = clock.now()` and `session_date` from the calendar (`OUTSIDE_SESSION` if none). A bar is flagged `GAP_BEFORE` when the previous stored bar for that symbol and session is more than 1 min earlier; the gap also goes in `report.gaps`. `OUT_OF_ORDER` is set when the provider yields a ts earlier than the previous one. A PK conflict with identical values counts as a duplicate (ignored). Different values → the new row is not stored, and the existing row gets the `DUPLICATE_CONFLICT` flag. **Missing bars are never synthesized.**
  - `ingest_quotes(...)` (the same pattern; flags NON_POSITIVE_PRICE, and `OHLC_INCONSISTENT` reused for `ask < bid`) and `ingest_corporate_actions(...)`
  - `scripts/make_synthetic_fixtures.py --date 2026-10-19 --symbols SPY,QQQ,AAPL --seed 7 --out DIR`. Generates a seeded random walk: 390 one-minute bars per symbol from 13:30Z, volume 5,000–50,000, one quote per minute at `ts = bar_start + 59s` with a 1–3 bp spread, and the header comment `# SYNTHETIC — not market data`. The `gaps` fixture is the same but SPY drops 14:00–14:04, and quotes stop entirely at 15:00Z.

- [ ] **Step 1: Generate the fixtures:** `uv run python scripts/make_synthetic_fixtures.py ...` for both directories. Commit the outputs (they are deterministic; re-running gives identical bytes; test `test_fixture_generator_is_deterministic`).
- [ ] **Step 2: Write the failing tests:** `test_csv_naive_timestamp_rejected` (line `SPY,2026-10-19 09:31:00,...` → `DataFormatError` mentioning `bars.csv:2`); `test_csv_parses_decimals_exactly`; `test_ingest_inserts_and_is_idempotent` (second run: inserted 0, duplicates 1170); `test_gap_detected_not_filled` (gaps fixture: one gap SPY 14:00–14:05, and no rows exist for 14:00–14:04); `test_non_positive_and_inconsistent_flagged`; `test_outside_session_flagged`; `test_conflicting_duplicate_flags_existing`; `test_unknown_symbol_reported_not_inserted`; `test_crossed_quote_flagged`; `test_corporate_actions_ingested`.
- [ ] **Step 3: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(data): provider protocol, csv replay provider, quality-flagged ingestion`.

---

### Task 16: Point-in-time market snapshots

**Files:**
- Create: `src/trading/data/snapshot.py`
- Test: `tests/integration/data/test_snapshot.py`

**Interfaces:**
- Consumes: `MarketBar`, `MarketQuote`, `MarketSnapshotRow`, `Bar`, `Quote`, `InstrumentInfo`, `content_hash`, `BLOCKING_FLAGS`
- Produces:
  - `SymbolView` (frozen: `symbol, instrument_id: UUID, last_bar: Bar | None, quote: Quote | None, bar_age_seconds: int | None, stale: bool, tradable: bool`)
  - `MarketSnapshot` (frozen: `id: UUID, as_of: datetime, provider: str, symbols: Mapping[str, SymbolView], quality_status: Literal["OK","DEGRADED","STALE"], data_hash: str`)
  - `build_snapshot(session, *, symbols: Sequence[str], as_of: datetime, provider: str, max_bar_age_seconds: int) -> MarketSnapshot`. **Point-in-time rule:** consider only bars where `ts + 1min <= as_of AND ingested_at <= as_of` and quotes where `ts <= as_of AND ingested_at <= as_of`. Take the latest of each per symbol. `bar_age_seconds = as_of − available_at`. `stale = age > max_bar_age_seconds or no bar`. `tradable = not stale and last_bar has no BLOCKING flag`. Quality: all tradable → OK; none tradable → STALE; otherwise DEGRADED. It persists the row with `content_json` and `data_hash = content_hash(content without id)`.
  - `load_snapshot(session, snapshot_id: UUID) -> MarketSnapshot`

- [ ] **Step 1: Write the failing tests**

```python
def test_bar_not_visible_until_closed(...): ...       # bar ts 13:45; as_of 13:45:59 → last_bar.ts == 13:44; as_of 13:46:00 → 13:45
def test_late_ingested_data_invisible(...): ...       # bar ingested_at 14:10, as_of 14:00 → not used
def test_stale_boundary_90s(...): ...                 # available 13:46:00; as_of 13:47:30 → age 90, fresh; 13:47:31 → stale
def test_blocking_flag_not_tradable(...): ...
def test_quality_status_mixed_degraded(...): ...
def test_hash_deterministic_and_roundtrip(...): ...   # build twice with the same data → equal data_hash; load_snapshot(id) == built
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(data): point-in-time market snapshots with staleness`.

---

### Task 17: Order state machine

**Files:**
- Create: `src/trading/execution/state_machine.py`
- Test: `tests/unit/execution/test_state_machine.py`

**Interfaces:**
- Produces: `OrderStatus(StrEnum)`: CREATED, RISK_APPROVED, SUBMITTING, ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELED, EXPIRED, UNKNOWN; `TERMINAL = {FILLED, REJECTED, CANCELED, EXPIRED}`; `OPEN_AT_BROKER = {ACKNOWLEDGED, PARTIALLY_FILLED}`; `ALLOWED: Mapping[OrderStatus, frozenset[OrderStatus]]`; `can_transition(a, b) -> bool`; `assert_transition(a, b) -> None` raises `IllegalTransition(TradingError)`.
- The table is exactly this:
  - CREATED → {RISK_APPROVED, REJECTED}
  - RISK_APPROVED → {SUBMITTING, REJECTED, CANCELED}
  - SUBMITTING → {ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, REJECTED, UNKNOWN}
  - ACKNOWLEDGED → {PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED, REJECTED, UNKNOWN}
  - PARTIALLY_FILLED → {PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED, UNKNOWN}
  - UNKNOWN → {ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED, REJECTED}
  - terminal → ∅

- [ ] **Step 1: Write the failing test.** `test_transition_table` is parametrized over all 100 `(a, b)` pairs, comparing against an expected-set literal in the test. Also `test_terminal_states_have_no_exits` and `test_illegal_transition_raises_with_both_states_in_message`.
- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(execution): canonical order state machine`.

---

### Task 18: Paper fill model (pure)

**Files:**
- Create: `src/trading/brokers/paper/fill_model.py`
- Test: `tests/unit/brokers/test_fill_model.py`

**Interfaces:**
- Consumes: `Bar`, `Quote`, `InstrumentInfo`, `qty_increment`, `PaperBrokerConfig`, `floor_to_increment`, `ceil_to_increment`, `quantize_cash`
- Produces:
  - `VenueOrderView` (frozen: `client_order_id, symbol, side: Side, order_type: OrderType, limit_price: Decimal | None, qty, filled_qty: Decimal, eligible_at: datetime, fill_seq: int`)
  - `FillDecision` (frozen: `execution_id: str, qty, price, fees, slippage_estimate: Decimal`)
  - `estimate_fees(side: Side, qty: Decimal, price: Decimal, cfg) -> Decimal` = `commission_per_order_usd` + (SELL only: `qty*price*sell_fee_rate` + `min(qty*taf_per_share_usd, taf_max_per_trade_usd)`), via `quantize_cash(…, ROUND_UP)`
  - `evaluate_fill(order: VenueOrderView, *, quote: Quote | None, bar: Bar | None, now: datetime, instrument: InstrumentInfo, cfg: PaperBrokerConfig) -> FillDecision | None`. Rules, in order:
    1. `now < eligible_at` → None
    2. no quote, `now − quote.ts > quote_max_age_seconds`, `bid <= 0`, or `ask < bid` → None
    3. BUY: `price = ceil_to_increment(ask * (1 + slippage_bps/10000), tick)`. SELL: `price = floor_to_increment(bid * (1 − slippage_bps/10000), tick)`.
    4. LIMIT: BUY needs `price <= limit`; SELL needs `price >= limit`; otherwise None
    5. no bar or `bar.volume == 0` → None. `liquidity = bar.volume * max_participation_rate`, also capped by `ask_size`/`bid_size` when present.
    6. `qty = floor_to_increment(min(order.qty − order.filled_qty, liquidity), qty_increment(...))`; `qty <= 0` → None
    7. `execution_id = f"{client_order_id}-{fill_seq + 1}"`; `slippage_estimate = quantize_cash(abs(price − mid) * qty)`; fees per `estimate_fees`
    8. **The model never uses bar high/low/close as the fill price.**

- [ ] **Step 1: Write the failing tests** (cfg: slippage 2 bps, participation 0.05, latency 500 ms, quote age 5 s)

```python
def test_buy_limit_fills_at_ask_plus_slippage(): d = evaluate_fill(order(BUY, LIMIT, limit="100.05", qty="0.5"), quote=q(bid="99.99", ask="100.00"), bar=b(volume="1000"), now=T+1s, ...); assert (d.price, d.qty) == (Decimal("100.02"), Decimal("0.5"))
def test_price_rounds_up_to_tick_for_buys(): ...      # ask 100.01 → 100.04
def test_limit_not_reached_no_fill(): ...             # limit 100.01, ask 100.00 → slipped 100.02 > limit → None
def test_no_fill_before_latency(): ...
@pytest.mark.parametrize("bad", ["missing", "stale_6s", "crossed", "zero_bid"])
def test_no_fill_on_bad_quote(bad): ...
def test_partial_fill_capped_by_participation(): ...  # qty 10, volume 100 → 5; then filled 5, seq 1 → 5 more, execution_id "...-2"
def test_whole_share_instrument_floors(): ...         # fractional_eligible False, liquidity 2.7 → 2
def test_never_fills_at_bar_low(): ...                # bar low 95, ask 100 → price 100.02
def test_sell_fees(): assert estimate_fees(SELL, Decimal("10"), Decimal("100"), cfg) == Decimal("0.0295")   # 1000*0.0000278=0.0278 + 10*0.000166=0.00166 → 0.02946 → ROUND_UP 0.0295
def test_buy_has_no_regulatory_fees(): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(paper): deterministic fill model with spread, slippage, latency, participation`.

---

### Task 19: BrokerAdapter, paper venue and PaperBroker with fault injection

**Files:**
- Create: `src/trading/brokers/base.py`, `src/trading/brokers/paper/{broker.py,faults.py}`
- Test: `tests/integration/brokers/test_paper_broker.py`

**Interfaces:**
- Consumes: `evaluate_fill`, `VenueOrderView`, venue ORM models, `trade_cash_amount`, `InstrumentInfo`, `Clock`
- Produces:
  - `BrokerOrderRequest` (frozen: `client_order_id, symbol, side, order_type, qty, limit_price: Decimal | None, time_in_force`)
  - `BrokerOrderStatus(StrEnum)` ACCEPTED/PARTIALLY_FILLED/FILLED/CANCELED/EXPIRED/REJECTED
  - `BrokerOrderState` (frozen: `client_order_id, broker_order_id, status, qty, filled_qty, avg_fill_price: Decimal | None, updated_at`)
  - `BrokerFill` (frozen: `execution_id, client_order_id, broker_order_id, symbol, side, qty, price, fees, slippage_estimate, ts`)
  - `BrokerAccountState` (frozen: `cash: Decimal, positions: Mapping[str, Decimal], as_of`)
  - Errors (all `TradingError`): `BrokerTimeout`, `BrokerUnavailable`, `BrokerRejected(reason: str)`
  - Protocol `BrokerAdapter`: `submit_order(req) -> BrokerOrderState`; `cancel_order(client_order_id) -> BrokerOrderState`; `get_order(client_order_id) -> BrokerOrderState | None`; `list_fills(*, since: datetime | None = None) -> Sequence[BrokerFill]` (ordered by ts, execution_id); `get_account() -> BrokerAccountState`; `list_open_orders() -> Sequence[BrokerOrderState]`
  - `FaultPlan` (frozen: `timeout_after_accept: frozenset[str] = frozenset(), timeout_before_accept: frozenset[str] = frozenset(), unavailable: bool = False`), keyed by client_order_id
  - `PaperBroker(session_factory, *, account_ref: str, config: PaperBrokerConfig, instruments: Mapping[str, InstrumentInfo], clock: Clock, faults: FaultPlan = FaultPlan())`. It implements `BrokerAdapter` and **uses its own transactions only** (it behaves like an external system). Extra methods: `open_account(starting_cash: Decimal) -> None` (idempotent); `process_tick(*, quotes: Mapping[str, Quote], bars: Mapping[str, Bar]) -> list[BrokerFill]` (evaluates every open venue order in `client_order_id` order; persists venue fills and order progress); `expire_day_orders(*, session_close: datetime) -> int`.
  - Venue-side checks on submit, each a `BrokerRejected` reason: `DUPLICATE_CLIENT_ORDER_ID`; `INSUFFICIENT_FUNDS` (venue cash − open-buy reservations < qty × (limit or latest ask) + est. fees); `INSUFFICIENT_POSITION` (sell > venue position − open sells); `UNKNOWN_SYMBOL`. `eligible_at = now + latency_ms`. Venue cash and positions are **derived from `venue_fills`**, never from the app ledger.

- [ ] **Step 1: Write the failing tests:** `test_submit_then_fill_on_tick`; `test_partial_fills_across_ticks_then_filled`; `test_cancel_partially_filled_keeps_fills`; `test_day_orders_expire_at_close`; `test_timeout_after_accept_order_exists` (raises `BrokerTimeout`; `get_order` → ACCEPTED); `test_timeout_before_accept_order_absent`; `test_unavailable_raises`; `test_duplicate_client_order_id_rejected`; `test_insufficient_funds_rejected`; `test_sell_beyond_position_rejected`; `test_execution_ids_deterministic_across_fresh_broker_instances`; `test_account_state_from_fills` (cash = 1000 − buys − fees + sells).
- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(paper): broker adapter protocol, paper venue, fault injection`.

---

### Task 20: Portfolio ledger, settlement, valuation, corporate actions (acceptance test 7)

**Files:**
- Create: `src/trading/portfolio/{ledger.py,balances.py,valuation.py,corporate_actions.py}`
- Test: `tests/integration/portfolio/test_ledger.py`, `test_valuation.py`, `test_corporate_actions.py`, `tests/integration/portfolio/test_ledger_properties.py`

**Interfaces:**
- Consumes: `BrokerFill`, `assert_transition`, `OrderStatus`, `trade_cash_amount`, `quantize_cash`, `quantize_price`, `MarketCalendar.add_sessions`
- Produces:
  - `LedgerInvariantError(TradingError)`
  - `open_paper_account(session, *, name: str, starting_cash: Decimal, now) -> UUID` (account with mode PAPER, `INITIAL_DEPOSIT` settled today, `peak_equity = starting_cash`)
  - `AppliedFill` (frozen: `fill_id: UUID, order_id: UUID, order_status: OrderStatus, remaining_qty: Decimal, position_qty: Decimal, cash_delta: Decimal`)
  - `apply_fill(session, *, account_id: UUID, fill: BrokerFill, settles_on: date, now) -> AppliedFill | None`. It locks the order by `client_order_id` `FOR UPDATE`. A duplicate `execution_id` returns None with no effect. `fill.qty > qty − filled_qty` raises `LedgerInvariantError`. Otherwise:
    - insert the fill
    - cash ledger: `TRADE` = `trade_cash_amount(side, qty, price)`, plus `FEE` = `−fees` if fees > 0, both with `settles_on`
    - position: BUY → weighted `average_cost` (`quantize_price`); SELL → `realized_pnl += quantize_cash((price − avg) × qty)`, and qty must stay ≥ 0 (else `LedgerInvariantError`)
    - every fill: `realized_pnl −= fees`
    - order: `filled_qty`, `avg_fill_price`, status → PARTIALLY_FILLED or FILLED (via `assert_transition`), an `order_events` row, and outbox `fill_applied`
  - `reverse_ledger_entry(session, *, entry_id: UUID, reason: str, now) -> UUID`
  - `CashBalances` (frozen: `total, settled, unsettled: Decimal`); `cash_balances(session, account_id, *, as_of_date: date) -> CashBalances` (settled = Σ amount where `settles_on <= as_of_date`)
  - `reserved_for_open_buys(session, account_id, *, ask_by_symbol: Mapping[str, Decimal], cfg: PaperBrokerConfig) -> Decimal` (Σ over open BUY orders of remaining × (limit or ask) + estimated fees)
  - `PositionView` (frozen: `symbol, instrument_id, sector, qty, average_cost, mark, market_value, unrealized_pnl`)
  - `PortfolioState` (frozen: `account_id, as_of, cash, settled_cash, reserved_cash, buying_power, equity, gross_exposure, exposure_by_symbol: Mapping[str, Decimal], exposure_by_sector: Mapping[str, Decimal], realized_pnl, unrealized_pnl, positions: tuple[PositionView, ...]`). `buying_power = settled_cash − reserved_cash` (since `use_unsettled_funds` is always False); `equity = cash + Σ market_value`.
  - `MissingMark(TradingError)`; `value_portfolio(session, account_id, *, marks: Mapping[str, Decimal], ask_by_symbol: Mapping[str, Decimal], as_of: datetime, session_date: date, cfg: PaperBrokerConfig) -> PortfolioState` (raises `MissingMark` if any position with qty > 0 has no mark)
  - `record_equity_snapshot(session, state: PortfolioState, *, kind: Literal["INTRADAY","SESSION_START","SESSION_END"]) -> UUID`
  - `apply_corporate_action(session, *, account_id, action: CorporateAction, now) -> bool`. Idempotent: a marker audit event `corporate_action.applied` with target = action id. SPLIT: `qty × ratio_to/ratio_from`, `avg ÷ same`. CASH_DIVIDEND: a `DIVIDEND` ledger entry of `qty × cash_amount`. Returns False if already applied.

- [ ] **Step 1: Write the failing tests**

```python
def test_partial_fills_update_cash_position_fees_remaining(db, order_factory):   # acceptance test 7
    o = order_factory(side=BUY, qty="1.0", limit="100.05", status=ACKNOWLEDGED)
    a1 = apply_fill(..., fill=bf(o, "e-1", qty="0.4", price="100.02", fees="0"), ...)
    assert a1.order_status is OrderStatus.PARTIALLY_FILLED and a1.remaining_qty == Decimal("0.6")
    a2 = apply_fill(..., fill=bf(o, "e-2", qty="0.6", price="100.03", fees="0.01"), ...)
    assert a2.order_status is OrderStatus.FILLED
    assert cash_balances(...).total == Decimal("899.9640")       # 1000 − 40.0080 − 60.0180 − 0.0100
    assert position(db).qty == Decimal("1.0") and position(db).average_cost == Decimal("100.02600000")
def test_duplicate_execution_id_ignored(...): ...
def test_overfill_raises(...): ...
def test_oversell_raises(...): ...
def test_rejected_order_has_no_effect(...): ...          # cash and positions unchanged; the ledger only has INITIAL_DEPOSIT
def test_realized_pnl_round_trip(...): ...               # buy 1 @100, sell 1 @101 with sell fee 0.01 → realized 0.99; equity 1000.99
def test_sale_proceeds_unsettled_until_t_plus_1(...): ... # sell on 2026-10-19 → settled excludes proceeds on the 19th, includes them on the 20th
def test_buying_power_subtracts_open_buy_reservations(...): ...
def test_value_portfolio_missing_mark_raises(...): ...
def test_reversal_entry_restores_balance(...): ...
def test_split_and_dividend(...): ...                    # 2:1 split doubles qty, halves avg; dividend 0.50 × qty credited once, idempotent
@given(fill_sequences())                                 # hypothesis: random buy/sell sequences that never oversell
def test_equity_identity(seq): assert abs((equity - start) - (realized + unrealized)) <= Decimal("0.0001") * len(seq)
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(portfolio): immutable ledger, settlement, valuation, corporate actions`.

---

### Task 21: Risk engine — deterministic checks (acceptance tests 3 and 4)

**Files:**
- Create: `src/trading/risk/models.py`, `src/trading/risk/engine.py`
- Test: `tests/unit/risk/test_engine.py`, `tests/unit/risk/test_engine_boundaries.py`

**Interfaces:**
- Consumes: `RiskPolicy`, `PaperBrokerConfig`, `MarketCalendar`, `MarketSnapshot`, `PortfolioState`, `InstrumentInfo`, `qty_increment`, `estimate_fees`, `RejectionCode`, `TradingState`, `RiskState`
- Produces:
  - `ProposedOrder` (frozen: `account_id: UUID, instrument: InstrumentInfo, side: Side, quantity: Decimal, order_type: OrderType, limit_price: Decimal | None, time_in_force: TimeInForce, strategy_id: str, signal_id: UUID | None, signal_expires_at: datetime | None, dedupe_key: str`)
  - `AccountView` (frozen: `portfolio: PortfolioState | None, trading_state: TradingState, risk_state: RiskState, open_buy_notional_by_symbol: Mapping[str, Decimal], open_sell_qty_by_symbol: Mapping[str, Decimal], new_entries_today: int, consumed_dedupe_keys: frozenset[str], enabled_strategies: frozenset[str]`)
  - `RiskReason` (frozen: `code: RejectionCode, detail: str`); `RiskEvaluation` (frozen: `allowed: bool, reasons: tuple[RiskReason, ...], limits_version: str, is_entry: bool, notional: Decimal | None`)
  - Protocol `RiskEngine.evaluate(proposal, account, market, *, now) -> RiskEvaluation`; `DeterministicRiskEngine(policy, *, calendar, paper_cfg, limits_version: str)`
- Rules. `is_entry = side is BUY` (long-only MVP). Every check runs. Reasons are sorted by `RejectionCode` declaration order. `allowed = not reasons`. `ref_price = limit_price` for LIMIT, else `quote.ask`. `notional = quantity × ref_price`.

| Code | Condition |
|------|-----------|
| TRADING_PAUSED | `trading_state is not PAPER_RUNNING` |
| RISK_STATE_UNKNOWN | `risk_state is UNKNOWN`, or `portfolio is None`, or symbol missing from snapshot |
| RECONCILIATION_REQUIRED | `risk_state is RECONCILIATION_REQUIRED` |
| DAILY_LOSS_LIMIT | `risk_state is HALTED_DAILY_LOSS and is_entry` |
| DRAWDOWN_LIMIT | `risk_state is HALTED_DRAWDOWN and is_entry` |
| MARKET_CLOSED | `calendar.session_at(now)` is None (detail `closed`); `CalendarUnavailable` (detail `calendar_unavailable`); entry with `close_at − now <= 15 min` (detail `entry_cutoff`) |
| SIGNAL_EXPIRED | `signal_expires_at is not None and now >= signal_expires_at` |
| STALE_MARKET_DATA | the symbol view is `stale` or not `tradable` |
| INVALID_QUOTE | no quote, `bid <= 0`, `ask < bid`, or `spread_bps > max_spread_bps` |
| STRATEGY_DISABLED | `strategy_id not in enabled_strategies` |
| DUPLICATE_INTENT | `dedupe_key in consumed_dedupe_keys` |
| UNSUPPORTED_ORDER | `order_type`/`tif` not in {LIMIT, MARKET}/{DAY}, LIMIT without a price, or `asset_type` not EQUITY/ETF |
| SHORT_NOT_ALLOWED | SELL with `quantity > position qty − open_sell_qty` |
| INVALID_TICK_SIZE | limit price not a multiple of `tick_size`, or ≤ 0 |
| INVALID_QUANTITY | `quantity <= 0` or not a multiple of `qty_increment` |
| EXPOSURE_LIMIT | entries only, post-trade vs. `equity`: symbol (`market_value + open_buy_notional + notional > 0.10 × equity`, detail `symbol`), then sector (`> 0.30`, detail `sector`), then gross (`> 0.80`, detail `gross`); one reason per breached limit |
| INSUFFICIENT_CASH | entry with `notional + est_fees > buying_power` |
| CASH_RESERVE_LIMIT | entry with `cash − reserved − notional − est_fees < 0.20 × equity` |
| MAX_DAILY_ENTRIES | entry with `new_entries_today >= 10` |
| MAX_OPEN_SYMBOLS | entry for a symbol not currently held or ordered, with `len(open symbols) >= 3` |

- [ ] **Step 1: Write the failing tests.** Use a `make_ctx(**overrides)` factory: equity 1000, cash 1000, no positions, PAPER_RUNNING, NORMAL, now `2026-10-19T15:00Z`, a fresh SPY snapshot with bid 99.99 / ask 100.00, LIMIT BUY 1 @ 100.00.

```python
def test_baseline_allowed(): assert evaluate(make_ctx()).allowed
def test_symbol_cap_boundary_exact_allowed(): assert evaluate(make_ctx(qty="1", limit="100.00")).allowed          # exactly 10%
def test_symbol_cap_uses_exact_quantity(): r = evaluate(make_ctx(qty="1.0001", limit="100.00")); assert reasons(r) == [("EXPOSURE_LIMIT", "symbol")]
def test_sector_cap(): ...       # policy symbol 0.20; hold AAPL 150 + MSFT 140 (Technology); buy NVDA... → ("EXPOSURE_LIMIT","sector")
def test_gross_cap(): ...        # policy symbol/sector 1.0, gross 0.80; existing 750 + new 60 → reasons include ("EXPOSURE_LIMIT","gross")
def test_cash_reserve(): ...
def test_insufficient_cash_counts_open_buy_reservations(): ...
def test_expired_signal_rejected(): r = evaluate(make_ctx(signal_expires_at=NOW)); assert codes(r) == ["SIGNAL_EXPIRED"]          # acceptance test 3
def test_stale_bar_rejected_at_91s(): ...                          # acceptance test 3; and 90 s is allowed
def test_wide_spread_rejected(): ...                               # bid 99.70 / ask 100.00 ≈ 30 bps > 25
def test_entry_cutoff_last_15_minutes(): ...                       # now 19:45:00Z → MARKET_CLOSED entry_cutoff; 19:44:59Z allowed
def test_exit_allowed_in_cutoff_and_during_daily_halt(): ...       # SELL held qty at 19:50Z with HALTED_DAILY_LOSS → allowed
def test_unknown_and_reconciliation_block_exits(): ...
def test_short_sell_rejected(): ...
def test_tick_and_quantity_validation(): ...                       # limit 100.005 → INVALID_TICK_SIZE; qty 0.00005 → INVALID_QUANTITY; whole-share instrument qty 0.5 → INVALID_QUANTITY
def test_max_entries_and_open_symbols(): ...
def test_options_asset_rejected(): ...                             # instrument with an unsupported asset type, built by bypassing validation → UNSUPPORTED_ORDER
def test_multiple_reasons_in_stable_order(): ...                   # paused + expired + stale → ["TRADING_PAUSED","SIGNAL_EXPIRED","STALE_MARKET_DATA"]
def test_missing_portfolio_fails_closed(): ...
def test_engine_is_pure(): ...                                     # same inputs twice → equal evaluations; inputs unmodified
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(risk): deterministic risk engine with stable rejection codes`.

---

### Task 22: Risk decision recording, session state and halts (acceptance test 5)

**Files:**
- Create: `src/trading/risk/service.py`, `src/trading/risk/halts.py`; modify `cli.py` (`trading admin clear-drawdown-halt --reason TEXT`)
- Test: `tests/integration/risk/test_service.py`, `tests/integration/risk/test_halts.py`

**Interfaces:**
- Consumes: `DeterministicRiskEngine`, `set_risk_state`, `get_system_state`, `record_audit`, `emit_event`, `TradingSession`, `Account.peak_equity`
- Produces:
  - `RecordedDecision` (frozen: `decision_id: UUID, evaluation: RiskEvaluation`)
  - `build_account_view(session, *, account_id, portfolio: PortfolioState | None, session_date: date, config: AppConfig) -> AccountView` (enabled strategies come from config; consumed dedupe keys = `risk_decisions.dedupe_key` of allowed decisions already referenced by an `order_intents` row)
  - `evaluate_and_record(session, engine: RiskEngine, *, proposal, account: AccountView, market: MarketSnapshot, portfolio_snapshot_id: UUID | None, now) -> RecordedDecision` (inserts an immutable `risk_decisions` row with `reasons_json`, `limits_version`, `market_snapshot_id`, and `proposal_json`)
  - `start_session(session, *, account_id, session_date, start_equity: Decimal, now) -> None`. Inserts `trading_sessions` (idempotent). If risk is `HALTED_DAILY_LOSS` → `NORMAL` with reason `new_session` (C6).
  - `evaluate_halts(session, *, account_id, state: PortfolioState, session_date, policy: RiskPolicy, now) -> RiskState`:
    - updates `accounts.peak_equity = max(peak, equity)`
    - no `trading_sessions` row → `UNKNOWN`
    - `peak − equity >= 0.05 × peak` → `HALTED_DRAWDOWN`
    - else `start − equity >= 0.01 × start` → `HALTED_DAILY_LOSS` (sets `daily_loss_halted_at`)
    - never overrides `RECONCILIATION_REQUIRED`/`UNKNOWN`; `HALTED_DRAWDOWN` is never downgraded here
    - any transition emits outbox `risk_alert`
  - `clear_drawdown_halt(session, *, actor, reason, request_id, now) -> SystemState` (only from `HALTED_DRAWDOWN` → `NORMAL`; audited; resets `peak_equity` to current equity)

- [ ] **Step 1: Write the failing tests**

```python
def test_decision_recorded_immutably_with_reasons(...): ...
def test_daily_loss_boundary(...):        # start 1000: equity 990.01 → NORMAL; 990.00 → HALTED_DAILY_LOSS (loss == 1% halts, C4)
def test_daily_halt_persists_across_restart(database_url, ...):   # acceptance test 5
    # halt with session_factory A; dispose engine A; build a fresh engine/factory/engine instance B
    # assert get_system_state(B).risk_state is HALTED_DAILY_LOSS
    # evaluate BUY via B → DAILY_LOSS_LIMIT; SELL of held qty → allowed
def test_daily_halt_clears_next_session(...): ...
def test_drawdown_halt_requires_operator(...): ...       # peak 1000 → equity 950.00 → HALTED_DRAWDOWN; start_session next day keeps it; clear_drawdown_halt → NORMAL, audited
def test_halt_emits_risk_alert_outbox(...): ...
def test_halts_do_not_override_reconciliation_required(...): ...
def test_duplicate_intent_after_consumption(...): ...    # an allowed decision with dedupe_key k and an intent → the next evaluation with k → DUPLICATE_INTENT
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(risk): recorded decisions, persistent daily-loss and drawdown halts`.

---

### Task 23: Execution manager — sole writer of intents and orders

**Files:**
- Create: `src/trading/execution/manager.py`
- Test: `tests/integration/execution/test_manager.py`

**Interfaces:**
- Consumes: `BrokerAdapter`, `assert_fence`, `Lease`, `assert_transition`, `apply_fill`, `make_client_order_id`, `RecordedDecision`, `MarketCalendar`, `emit_event`, `ORDERS_BY_STATUS`
- Produces:
  - Errors (all `TradingError`): `RiskNotApproved`, `DecisionAlreadyUsed`
  - `ExecutionManager(session_factory, broker: BrokerAdapter, *, clock: Clock, lease: Lease, calendar: MarketCalendar, config: AppConfig)`. **Every write transaction begins with `assert_fence(session, lease, now=clock.now())`.**
  - `create_order(session, *, proposal: ProposedOrder, decision: RecordedDecision, market_snapshot_id: UUID, session_date: date) -> UUID`. Raises `RiskNotApproved` if `not decision.evaluation.allowed`, and `DecisionAlreadyUsed` on a unique violation of `risk_decision_id`. `client_order_id = make_client_order_id(proposal.strategy_id, session_date, n)`, where `n` = 1 + the count of intents for that strategy and date, under `SELECT … FOR UPDATE` of the `trading_sessions` row. Inserts `order_intents` + `orders` (CREATED) + `order_events`. Increments `trading_sessions.new_entries` for entries. Emits outbox `order_created`.
  - `submit(order_id: UUID) -> OrderStatus`:
    - **Transaction A:** fence; lock the order; it must be CREATED (otherwise return its current status and **do not call the broker**). Re-check: decision allowed; `now < intent.expires_at` (else REJECTED `SIGNAL_EXPIRED`); `system_state.trading_enabled`, or for exits `trading_state is PAPER_RUNNING and risk_state in {NORMAL, HALTED_DAILY_LOSS, HALTED_DRAWDOWN}` (else REJECTED with the matching code). Then → RISK_APPROVED → SUBMITTING. Commit.
    - **Broker call:** `submit_order`.
    - **Transaction B:** fence; lock; apply the result. ACCEPTED → ACKNOWLEDGED with `broker_order_id`; PARTIALLY_FILLED/FILLED → ACKNOWLEDGED, leaving the fills for `sync_fills`; `BrokerRejected` → REJECTED with the reason; `BrokerTimeout`/`BrokerUnavailable` → UNKNOWN. Add `order_events` + outbox.
  - `sync_fills() -> int`. For each `broker.list_fills(since=max local fill ts − 1 min)`, call `apply_fill` in its own fenced transaction, with `settles_on = calendar.add_sessions(fill session date, cfg.settlement_days)`. Then for local orders in `OPEN_AT_BROKER`, pick up broker terminal states CANCELED/EXPIRED via `get_order`. Returns the number of fills applied.
  - `cancel(order_id: UUID, *, reason: str) -> OrderStatus`

- [ ] **Step 1: Write the failing tests**

```python
def test_create_order_requires_allowed_decision(...): ...           # denied → RiskNotApproved; no rows
def test_decision_cannot_be_reused(...): ...
def test_client_order_ids_sequential_per_strategy_day(...): ...     # paper-orb_v1-20261019-0001, -0002
def test_submit_happy_path_acknowledged(...): ...
def test_double_submit_single_venue_order(...): ...                 # submit twice → the second returns ACKNOWLEDGED without a broker call; the venue has exactly 1 order
def test_broker_reject_marks_rejected_no_position(...): ...
def test_timeout_marks_unknown(...): ...                            # FaultPlan(timeout_after_accept={coid})
def test_expired_intent_rejected_before_broker(...): ...
def test_paused_rejects_entry_before_broker(...): ...
def test_exit_allowed_during_daily_halt(...): ...
def test_submit_with_stale_lease_never_reaches_broker(...): ...     # another holder takes the lease → LeaseLost; venue order count 0
def test_sync_fills_idempotent_and_partial(...): ...
def test_entry_count_incremented(...): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(execution): fenced execution manager with two-phase submit`.

---

### Task 24: Reconciliation (acceptance test 6)

**Files:**
- Create: `src/trading/execution/reconcile.py`
- Test: `tests/integration/execution/test_reconcile.py`, `tests/chaos/test_timeout_restart.py`

**Interfaces:**
- Consumes: `BrokerAdapter`, `apply_fill`, `assert_transition`, `set_risk_state`, `mark_reconciled`, `cash_balances`, `ReconciliationRun`
- Produces:
  - `Mismatch` (frozen: `kind: Literal["ORPHAN_BROKER_ORDER","MISSING_AT_BROKER","STATUS","FILLED_QTY","POSITION","CASH","LEDGER_INTERNAL"], subject: str, local: str, broker: str`)
  - `ReconciliationReport` (frozen: `run_id: UUID, status: Literal["OK","PENDING","MISMATCH","ERROR"], resolved: tuple[str, ...], pending: tuple[str, ...], mismatches: tuple[Mismatch, ...]`)
  - `reconcile_account(session_factory, *, account_id: UUID, broker: BrokerAdapter, clock: Clock, lease: Lease, calendar: MarketCalendar, policy: RiskPolicy) -> ReconciliationReport`. Algorithm, in order:
    1. Import every broker fill via `apply_fill` (idempotent).
    2. For each local order in {SUBMITTING, UNKNOWN} ∪ `OPEN_AT_BROKER`, call `get_order`:
       - found → move to the broker's status (ACCEPTED→ACKNOWLEDGED, etc.) and add to `resolved`
       - not found and status in {SUBMITTING, UNKNOWN}: if `now − updated_at >= unknown_order_grace_seconds` → REJECTED `NOT_FOUND_AT_BROKER` (resolved); else add to `pending`
       - not found and status in `OPEN_AT_BROKER` → mismatch `MISSING_AT_BROKER`
       - `filled_qty` differs after the fill import → mismatch `FILLED_QTY`
    3. Broker orders with no local order → `ORPHAN_BROKER_ORDER`. (uses `broker.list_open_orders()`)
    4. Positions: local `positions.qty` vs. `get_account().positions`, exact match.
    5. Cash: local ledger total vs. broker cash, with `abs diff <= 0.0001`.
    6. Internal invariants: Σ fills per order = `filled_qty`; Σ signed fill qty per instrument = position qty. A failure → `LEDGER_INTERNAL`.
    7. Result, which is **never auto-resubmitted** (C7):
       - OK → `mark_reconciled`; if risk is UNKNOWN or RECONCILIATION_REQUIRED → NORMAL (halts are preserved)
       - PENDING → if risk is NORMAL → RECONCILIATION_REQUIRED
       - MISMATCH or exception (ERROR) → RECONCILIATION_REQUIRED, plus outbox `risk_alert`
       - always persist `reconciliation_runs` and an audit event

- [ ] **Step 1: Write the failing tests**

```python
def test_timeout_then_restart_reconciles_without_duplicate(...):     # acceptance test 6 (tests/chaos)
    # manager A, FaultPlan(timeout_after_accept={coid}) → UNKNOWN
    # discard A; build a fresh PaperBroker (no faults) + a new lease holder (takeover) + manager B
    # reconcile → order ACKNOWLEDGED, report OK, risk NORMAL
    # venue tick fills it; reconcile → FILLED; venue order count for coid == 1; positions match
def test_submitting_crash_with_broker_record_adopted(...): ...        # Txn A committed, broker accepted, process "died" before Txn B
def test_submitting_crash_without_broker_record_rejected_after_grace(...): ...  # 29 s → PENDING + RECONCILIATION_REQUIRED; 30 s → REJECTED NOT_FOUND_AT_BROKER, OK, NORMAL
def test_missing_fill_imported(...): ...
def test_cash_mismatch_fails_closed(...): ...      # insert a venue adjustment fill directly → MISMATCH; risk RECONCILIATION_REQUIRED; BUY and SELL both rejected by the engine
def test_orphan_broker_order_detected(...): ...
def test_ok_reconcile_preserves_daily_halt(...): ...
def test_last_reconciled_at_updated(...): ...
```

- [ ] **Step 2: Run** (FAIL) → **implement** → **run** (PASS) → **commit** `feat(execution): fail-closed reconciliation with unknown-order resolution`.

---

### Task 25: Paper tick processor, calendar scheduling, worker wiring, deterministic replay (acceptance tests 2, 3 and 5 end to end)

**Files:**
- Create: `src/trading/execution/{intents.py,tick.py,replay.py}`; modify `src/trading/jobs/schedule.py`, `src/trading/services/{worker.py,handlers.py}`, `cli.py` (`trading replay`)
- Test: `tests/unit/jobs/test_schedule_calendar.py`, `tests/replay/test_replay_determinism.py`, `tests/replay/test_replay_rejections.py`, `tests/chaos/test_restart_mid_session.py`, `tests/integration/services/test_handlers.py`, `tests/fixtures/replay/scripts/{basic.yaml,expired.yaml}`

**Interfaces:**
- Consumes: everything from Tasks 13–24
- Produces:
  - `ProposalRequest` (frozen: `symbol, side, quantity, order_type, limit_price: Decimal | None, strategy_id: str, signal_id: UUID | None, signal_expires_at: datetime | None`)
  - Protocol `IntentSource.proposals(*, snapshot: MarketSnapshot, portfolio: PortfolioState, now: datetime) -> Sequence[ProposalRequest]`; `NullIntentSource`; `ScriptedIntentSource.from_yaml(path: Path)`. Script entries are `{at: ISO, symbol, side, quantity, order_type, limit_price, strategy_id, ttl_seconds}`. `signal_id` is a uuid5 of the entry, `signal_expires_at = at + ttl`, and dedupe key `f"{strategy_id}:{signal_id}"`. Proposals are emitted on the first tick ≥ `at`, and only once. **Plan B replaces this with strategies + allocator behind the same protocol.**
  - `TickResult` (frozen: `minute, snapshot_id, fills_applied: int, decisions: int, orders_submitted: int, risk_state: RiskState`)
  - `PaperTickProcessor(session_factory, *, account_id, broker: PaperBroker, manager: ExecutionManager, engine: RiskEngine, provider: MarketDataProvider | None, intents: IntentSource, calendar, config: AppConfig, clock: Clock)` with:
    - `start_session(d: date) -> None`: reconcile; value the portfolio; `start_session`; SESSION_START snapshot
    - `run_tick(minute: datetime) -> TickResult`:
      1. with a provider: ingest bars `[minute−1m, minute)` and quotes up to `minute`
      2. `build_snapshot(as_of=clock.now())`
      3. `broker.process_tick` with the latest quote and bar per symbol from the snapshot
      4. `manager.sync_fills()`
      5. `value_portfolio` (`MissingMark` → `set_risk_state(UNKNOWN)`) → INTRADAY snapshot → `evaluate_halts`
      6. `intents.proposals` → for each: build `ProposedOrder` → `evaluate_and_record` → if allowed, `create_order` + `submit`
    - `end_session(d: date) -> None`: `expire_day_orders(close)`; sync fills; reconcile; SESSION_END snapshot; open positions listed in the audit details as `unresolved_positions` (flattening comes in Plan B)
  - `plan_jobs` (extended). When `calendar.session_on(today)`:
    - `session_start:<date>` at `open − 30 min`
    - `paper_tick:<minute ISO>` for each minute `open < m <= close`, scheduled at `m + 5 s`
    - `session_end:<date>` at `close + 5 min`
    - Only jobs with `scheduled_at` in `[now − 2 min, now]` are returned. Missed older ticks are not backfilled, because stale ticks must not trade.
  - `build_handlers` adds `session_start`, `paper_tick` (if `clock.now() − minute > 90 s` → skip with audit `tick_skipped_stale`; `provider is None` → no-op logging `market_data: not_configured`), `session_end`, `reconcile`, `outbox_dispatch`. `run_worker`'s `startup_hooks = [reconcile_account]`, so the worker always reconciles before it processes any job.
  - `ReplayResult` (frozen: `result_hash: str, orders: int, fills: int, final_equity: Decimal, decisions: int`)
  - `replay_session(session_factory, *, fixtures_dir: Path, session_date: date, script: Path | None, config: AppConfig, stop_after: datetime | None = None, resume: bool = False) -> ReplayResult`. Uses a `ManualClock`, `CsvReplayProvider`, and a fresh paper account `replay-<date>` (unless `resume`). It sets `PAPER_RUNNING` via `set_trading_state`, then for each minute sets the clock to `minute + 5 s` and runs `run_tick`, finishing with `end_session`. `result_hash = content_hash` of the sorted orders (`client_order_id, status, qty, filled_qty, avg_fill_price`), fills (`execution_id, qty, price, fees`), ledger amounts in order, and final equity. **UUIDs and wall-clock times are excluded.**
  - CLI: `trading replay --fixtures DIR --date 2026-10-19 [--script FILE]` prints the ReplayResult as JSON.

- [ ] **Step 1: Write the scripts.** `basic.yaml`: BUY SPY 0.15 LIMIT at ~ask+0.05 at 14:00Z, SELL SPY 0.15 LIMIT at 18:00Z, BUY QQQ 0.1 at 15:00Z, all `ttl_seconds: 60`. `expired.yaml`: one entry with `ttl_seconds: 0`.
- [ ] **Step 2: Write the failing tests**

```python
def test_full_day_tick_count(): assert count_kind(all_jobs_for_day(date(2026, 10, 19)), "paper_tick") == 390
def test_half_day_tick_count(): assert count_kind(all_jobs_for_day(date(2026, 11, 27)), "paper_tick") == 210
def test_holiday_no_jobs(): assert count_kind(all_jobs_for_day(date(2026, 11, 26)), "paper_tick") == 0
def test_missed_ticks_not_backfilled(): ...
def test_replay_is_deterministic(session_factory, truncate):          # acceptance test 2
    r1 = replay_session(..., script=BASIC); truncate(); r2 = replay_session(..., script=BASIC)
    assert r1.result_hash == r2.result_hash and r1.fills >= 2 and r1.orders == 3
def test_expired_signal_creates_no_order(...):                         # acceptance test 3
    r = replay_session(..., script=EXPIRED); assert r.orders == 0 and decision_codes(db) == [["SIGNAL_EXPIRED"]]
def test_stale_quotes_create_no_order(...): ...                        # gaps fixture + a BUY at 15:30Z (quotes stopped at 15:00) → 0 orders; reasons include STALE_MARKET_DATA or INVALID_QUOTE
def test_ledger_reconciles_at_end_of_replay(...): ...                  # the last reconciliation_runs.status == "OK"
def test_restart_mid_session_matches_uninterrupted(...):               # tests/chaos
    full = replay_session(...); truncate()
    replay_session(..., stop_after=utc("2026-10-19T16:00")); drop all in-memory objects
    resumed = replay_session(..., resume=True)                          # new objects; reconcile on start; continue from 16:01
    assert resumed.result_hash == full.result_hash
def test_worker_reconciles_before_first_job(...): ...
def test_paper_tick_without_provider_is_noop(...): ...
def test_stale_tick_skipped(...): ...
```

- [ ] **Step 3: Run** (FAIL) → **implement** → **run** `uv run pytest tests/replay tests/chaos tests/unit/jobs tests/integration/services -v` (PASS) → **commit** `feat(execution): paper tick loop, calendar scheduling, deterministic replay`.

---

### Task 26: Read API, status enrichment, OpenAPI, documentation

**Files:**
- Create: `src/trading/api/routes/portfolio.py`, `docs/api-contracts/openapi.json`, `docs/architecture/overview.md`, `docs/runbooks/{recovery.md,replay.md}`, `docs/security/notes.md`; modify `src/trading/api/routes/{system.py,health.py}`, `cli.py` (`trading openapi`), `README.md`
- Test: `tests/integration/api/test_portfolio_api.py`, `tests/unit/test_architecture_boundaries.py`, `tests/unit/api/test_openapi_snapshot.py`

**Interfaces:**
- Consumes: `value_portfolio` inputs, the `EquitySnapshot` and `Order`/`FillRow` models, `XnysCalendar`
- Produces:
  - `GET /v1/portfolio` (viewer) → `{as_of, equity, cash, buying_power, realized_pnl, unrealized_pnl, exposure, currency: "USD"}` from the latest `equity_snapshots` row plus the current `cash_balances`/reservations. Money is serialized as strings. No snapshot → 503 `STATE_UNAVAILABLE`.
  - `GET /v1/positions?strategy_id=&cursor=` → `{items: [{symbol, qty, average_cost, mark, market_value, unrealized_pnl, strategy_ids}], next_cursor}`. `strategy_ids` is the union of the `order_intents.strategy_ids` behind the position's fills; the filter uses it.
  - `GET /v1/orders?status=&cursor=` → `{items: [{id, client_order_id, broker_order_id, symbol, side, type, limit_price, qty, filled_qty, avg_fill_price, status, status_reason, created_at, updated_at, fills: [{execution_id, ts, qty, price, fees}]}], next_cursor}`
  - `/v1/system/status` now fills `session` (`{date, open_at, close_at, is_open, is_half_day}` or null) and `last_data_at` (max `market_bars.ingested_at`). Readiness `market_data`: `closed` outside the session; `not_configured` if no provider is configured; `stale` if the last ingest is more than 120 s old in the session; else `ok`.
  - `trading openapi` writes the schema JSON to stdout.

- [ ] **Step 1: Write the failing tests**

```python
def test_portfolio_after_replay(...): ...        # values are strings; equity equals the last snapshot
def test_orders_include_fills_and_paginate(...): ...
def test_positions_strategy_filter(...): ...
def test_viewer_can_read_unauthenticated_cannot(...): ...
def test_no_trading_mutation_routes(app):
    mutating = {(m, r.path) for r in app.routes for m in getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}}
    assert mutating == {("POST", "/v1/control/pause"), ("POST", "/v1/control/resume-paper")}
def test_api_does_not_import_brokers():          # unit: walk the ast imports of every module under src/trading/api; none start with "trading.brokers" or "trading.execution.manager"
def test_openapi_snapshot_current(): assert json.loads(Path("docs/api-contracts/openapi.json").read_text()) == create_app(...).openapi()
```

- [ ] **Step 2: Run** (FAIL) → **implement**. Regenerate the snapshot with `uv run trading openapi > docs/api-contracts/openapi.json`. → **Run** (PASS).
- [ ] **Step 3: Write the docs.**
  - `README.md`: exact commands for setup (`uv sync`, `scripts/init-secrets.sh`, `docker compose up -d --build`, `docker compose run --rm api admin create-api-key --name me --role operator`), testing (`make lint type test smoke`), replay (`uv run trading replay --fixtures tests/fixtures/replay/2026-10-19 --date 2026-10-19 --script tests/fixtures/replay/scripts/basic.yaml`), and a paper run (resume-paper via curl with `Idempotency-Key`, noting it needs a data provider during market hours).
  - `docs/architecture/overview.md`: a mermaid diagram of the spec's flow, showing trust boundaries and which process can reach the broker adapter.
  - `docs/runbooks/recovery.md`: lease takeover, UNKNOWN orders, RECONCILIATION_REQUIRED, clearing a drawdown halt.
  - `docs/security/notes.md`: secrets, DB roles, append-only tables, auth, Tailscale only, no live path.
- [ ] **Step 4: Commit** `feat(api): portfolio, positions and orders read API; docs and openapi`.

**Plan A milestone:** run `make lint type test smoke` and `uv run trading replay …` twice (the hashes must be equal). Report the acceptance tests now covered (1–7, 11), the open decisions, and the next plan (B).
