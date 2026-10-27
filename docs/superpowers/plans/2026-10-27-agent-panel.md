# Agent Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An **Agents** view in the dashboard: per agent, numbers, rules, per-decision "why", reviewed config edits, and evidence-labelled suggestions.

**Architecture:** Read-only stats and a static agent registry feed new `/v1/agents/*` routes. Config edits are stored as proposals (new table, mirrors `strategy_config_proposals`); approved proposals are merged over `configs/agents.yaml` when a process starts, and the worker/scheduler restart cleanly after a session ends so they take effect only at a session boundary. A deterministic suggestion engine reads recorded runs and trades.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic (frozen models), Postgres (tests use testcontainers), framework-free JS in `src/trading/api/static/`.

**Spec:** `docs/superpowers/specs/2026-10-27-agent-panel-design.md` (mockup in `docs/superpowers/specs/assets/`).

## Global Constraints

- Repo: `/Users/dankadr/robinhood-trading`, branch lineage of `build/provider-cache`. Work on a new branch `feature/agent-panel`.
- Agents only propose. No change may loosen a risk control, switch mode, or touch live trading. Locked: everything under `risk`, `paper_broker`, `system`, `universe`, `strategies`, and `agents.llm`.
- Settings are validated through the existing frozen pydantic models in `trading/core/config.py` (e.g. macro `ttl_minutes > run_every_minutes`).
- Edits are applied only at a session boundary, never mid-session.
- Reads need `require_viewer`; writes need `require_operator`, are audited with `record_audit`, and are idempotent.
- Dynamic text in the dashboard uses `textContent` only (helpers in `util.js`); never `innerHTML`. The existing test in `tests/integration/api/test_dashboard_api.py` guards this.
- Prompt text is view-only. No floats in stored JSON (`Decimal`/`str`, as `proposals._reject_floats` does).
- Suggestions below `min_decisions=30` or `min_days=10` are `watching`, never proposable.
- Fast checks before every commit: `ruff check`, `ruff format --check`, `mypy src`, the touched tests. Check the **exit code**, not just output.
- Each commit ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- Zero runs for an agent (new/disabled): panel shows empty state, not an error or divide-by-zero.
- A run whose `result_json` is `{"error": ..., "input_hash": ...}` (REJECTED_INVALID): decisions list shows the error text, no crash on missing keys.
- LLM text containing `<script>` / HTML: rendered as text (extends the existing guard).
- Edit that violates a cross-field rule (macro `ttl_minutes <= run_every_minutes`): refused with a readable message, nothing stored.
- Two pending proposals for the same agent: newer supersedes only the same proposer's older one (never someone else's).
- Edit of a stale base (config changed since the proposal was written): refused.
- Approved proposal never applies while a session is open.

---

### Task 1: Agent registry (rules metadata)

**Files:**
- Create: `src/trading/agents/registry.py`
- Test: `tests/unit/agents/test_registry.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class AgentInfo: name: str; job: str; can_affect: tuple[str, ...]; cannot: tuple[str, ...]; uses_llm: bool; prompt_version: str; prompt_text: str | None; valid_output: str; on_failure: str; editable: tuple[str, ...]`; `AGENT_REGISTRY: Mapping[str, AgentInfo]` with keys `macro, fundamental, evaluator, consensus, technical`; `LOCKED_SCOPES: tuple[str, ...]`.
- `editable` lists the `AgentsConfig.<agent>` field names allowed in v1: macro `enabled, required, ttl_minutes, run_every_minutes, first_run_after_open_minutes, dependent_strategies`; fundamental `enabled, lookback_hours, max_headlines, ttl_hours`; evaluator/consensus/technical `enabled`.

- [ ] **Step 1: Write failing tests** — `test_registry_covers_every_agent_config_field`: for each agent, `set(AgentInfo.editable) ⊆ set(AgentsConfig.<agent> fields)` and every config field is editable; `test_prompt_versions_match_modules`: `prompt_version` equals each module's `PROMPT_VERSION`; `test_consensus_has_no_llm`: `uses_llm is False`, `prompt_text is None`.
- [ ] **Step 2: Run** `pytest tests/unit/agents/test_registry.py` — expect FAIL (module missing).
- [ ] **Step 3: Implement** `registry.py`. Copy rule wording from the mockup's "Rules" card and the spec. `prompt_text` is read from each agent module's existing prompt constant (find it next to `PROMPT_VERSION`).
- [ ] **Step 4: Run tests** — expect PASS.
- [ ] **Step 5: Commit** `feat(agents): registry of agent rules and editable settings`.

### Task 2: Stats and decisions read service

**Files:**
- Create: `src/trading/agents/stats.py`
- Test: `tests/integration/agents/test_stats.py`

**Interfaces:**
- Consumes: `AgentRun` model (`agent_name, model, prompt_version, status, started_at, completed_at, result_json`).
- Produces:
  - `@dataclass(frozen=True) class AgentSummary: name: str; runs: int; completed: int; rejected: int; failed: int; prompt_tokens: int; completion_tokens: int; last_run_at: datetime | None; last_error: str | None; health: Literal["ok","degraded","failing","idle"]`
  - `def agent_summary(session: Session, name: str, *, since: datetime) -> AgentSummary`
  - `@dataclass(frozen=True) class Decision: run_id: UUID; at: datetime; status: str; headline: str; confidence: Decimal | None; evidence: tuple[str, ...]; limitations: tuple[str, ...]; error: str | None; input_hash: str | None; affected_orders: tuple[str, ...]`
  - `def agent_decisions(session: Session, name: str, *, limit: int = 30) -> list[Decision]`
- `health`: `failing` if the last run is REJECTED_INVALID/FAILED, `degraded` if any non-COMPLETED in `since` window, `idle` if no runs, else `ok`.
- `affected_orders` (macro only): `client_order_id`s of entry orders for `dependent_strategies` created in `[started_at, result_json.valid_until)`. Other agents: empty tuple.

- [ ] **Step 1: Failing tests** (seed with the `agent_run` helper style from `test_dashboard_api.py`): `test_empty_agent_is_idle`; `test_rejected_run_surfaces_error_and_failing_health` (result `{"error": "reply does not match EvaluatorOutput...", "input_hash": "x"}`); `test_tokens_summed_from_result_json`; `test_macro_decision_lists_orders_inside_validity_window` (one order inside the window, one outside → only the first).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** with SQL aggregation; tolerate missing keys in `result_json`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `feat(agents): per-agent stats and decision history`.

### Task 3: Proposals table and model

**Files:**
- Create: `migrations/versions/0004_agent_config.py`
- Modify: `src/trading/db/models/research.py` (add model next to `StrategyConfigProposal`)
- Test: `tests/integration/db/test_agent_config_schema.py`

**Interfaces:**
- Produces ORM `AgentConfigProposal` (table `agent_config_proposals`): `id uuid, agent_name text, changes_json jsonb, base_json jsonb, rationale text, status text CHECK IN ('PENDING_REVIEW','APPROVED','REJECTED','APPLIED','SUPERSEDED'), source text CHECK IN ('OPERATOR','SUGGESTION'), evidence_json jsonb NULL, proposed_by text, created_at, reviewed_by, reviewed_at, applied_at`. Index on `(status, created_at)`. Grants follow how `0003` grants `strategy_config_proposals` to the app role.

- [ ] **Step 1: Failing test** — insert a valid row; an invalid `status` and an invalid `source` each raise `IntegrityError`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** migration (revision `0004`, down `0003`) and model. **Step 4: Run** — PASS (also run the repo's existing migration/schema tests). **Step 5: Commit** `feat(db): agent_config_proposals`.

### Task 4: Propose and review service

**Files:**
- Create: `src/trading/agents/config_proposals.py`
- Test: `tests/integration/agents/test_config_proposals.py`

**Interfaces:**
- Consumes: Task 1 `AGENT_REGISTRY`; Task 3 model; `AgentsConfig`; `record_audit`.
- Produces: `ProposalInvalid`, `ProposalConflict`, `ProposalNotFound` exceptions;
  - `def propose_agent_config(session, *, agent_name: str, changes: Mapping[str, Any], rationale: str, proposed_by: str, effective: AgentsConfig, now: datetime, source: str = "OPERATOR", evidence: Mapping[str, Any] | None = None) -> AgentConfigProposal`
  - `def review_agent_config(session, proposal_id: UUID, *, approve: bool, reviewer: str, now: datetime) -> AgentConfigProposal`
- Rules: unknown agent or field not in `editable` → `ProposalInvalid`; floats rejected; merged values validated by `AgentsConfig.model_validate` (so cross-field rules apply) → `ProposalInvalid` with the pydantic message; no-op change (equals current) → `ProposalInvalid`; `base_json` records old values; supersede only same-proposer pending proposals for that agent; audit `agent_config.propose|approve|reject`. Review of a non-pending proposal → `ProposalConflict`. On review, if current effective values differ from `base_json` → `ProposalConflict` (stale).

- [ ] **Step 1: Failing tests:** `test_valid_edit_stores_diff`; `test_locked_field_refused` (`{"llm": ...}`, and agent `risk`); `test_cross_field_rule_refused` (macro `ttl_minutes=20, run_every_minutes=30`); `test_noop_refused`; `test_float_refused`; `test_newer_supersedes_only_same_proposer`; `test_stale_base_refused_on_review`; `test_review_writes_audit`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** modelled on `quant/proposals.py`. **Step 4: Run** — PASS. **Step 5: Commit** `feat(agents): agent config proposals with review`.

### Task 5: Effective config and session-boundary apply

**Files:**
- Create: `src/trading/agents/effective_config.py`
- Modify: `src/trading/cli.py` (the worker, scheduler and api entry points that call `load_config`), `src/trading/services/handlers.py` (session_end), `src/trading/services/worker.py`, `src/trading/services/scheduler.py`, `src/trading/services/exit_codes.py`
- Test: `tests/integration/agents/test_effective_config.py`, `tests/unit/services/test_config_reload.py`

**Interfaces:**
- Produces:
  - `def load_effective_config(config_dir: Path, session_factory: sessionmaker[Session]) -> AppConfig` — `load_config`, then overlay `changes_json` of `APPROVED`/`APPLIED` proposals in `created_at` order onto `AgentsConfig`, re-validated; a proposal that no longer validates is skipped and logged (never crashes startup).
  - `def apply_approved_agent_config(session: Session, *, now: datetime) -> int` — marks `APPROVED` → `APPLIED` (sets `applied_at`), audited `agent_config.apply`; returns count.
  - `EXIT_CONFIG_RELOAD = 4` in `exit_codes.py` (supervisor restarts; `restart: unless-stopped`).
  - `def config_changed_since(session: Session, started_at: datetime) -> bool` — any proposal with `applied_at > started_at`.
- Behaviour: the worker `session_end` handler calls `apply_approved_agent_config` after the session is closed. Worker and scheduler record their start time and, between jobs (heartbeat loop), exit with `EXIT_CONFIG_RELOAD` when `config_changed_since` is true. Never exit while a job is running. Edits proposed/approved during a session therefore do nothing until after `session_end`.

- [ ] **Step 1: Failing tests:** `test_overlay_changes_macro_interval`; `test_invalid_override_is_skipped_not_fatal`; `test_pending_and_rejected_not_applied`; `test_apply_marks_applied_once` (second call returns 0); unit `test_config_changed_since` and `test_reload_exit_not_taken_while_job_running`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; swap `load_config(...)` for `load_effective_config(...)` in the three long-running entry points only (leave one-shot CLI commands on `load_config`). **Step 4: Run** the new tests plus `tests/integration/services` — PASS. **Step 5: Commit** `feat(agents): approved agent config applies at the next session boundary`.

### Task 6: Suggestion engine

**Files:**
- Create: `src/trading/agents/suggestions.py`
- Modify: `src/trading/core/config.py` (add `SuggestionsConfig(min_days: int = 10, min_decisions: int = 30)` and `AgentsConfig.suggestions`), `configs/agents.yaml`
- Test: `tests/integration/agents/test_suggestions.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Suggestion: agent: str; title: str; field: str | None; current: Any; proposed: Any | None; evidence_n: int; evidence_needed: int; status: Literal["watching","proposable"]; rationale: str`; `def suggest(session: Session, name: str, *, config: AgentsConfig, now: datetime) -> list[Suggestion]`.
- Rules (deterministic, no LLM): (macro) (a) suggestion on `run_every_minutes` when the regime flips between consecutive runs more than 50% of the time (`proposed` = current + 15, within the field's bounds); (b) informational suggestion, `proposed=None`, when `UNKNOWN` is over 25% of runs, showing mean realised P&L per entry sized under `UNKNOWN` versus other regimes (no config change: the cause is usually too little data early in the session); (evaluator) repeated `REJECTED_INVALID` → a visible "failing" suggestion pointing at the error (not a config change, `proposed=None`). Every suggestion carries `evidence_n` (decisions or round trips) and is `proposable` only when `evidence_n >= min_decisions` and the data spans `>= min_days` distinct session dates; otherwise `watching`.

- [ ] **Step 1: Failing tests:** `test_below_threshold_is_watching`; `test_flip_rate_over_threshold_with_enough_data_is_proposable`; `test_evaluator_failures_surface_without_config_change`; `test_no_data_returns_empty`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5: Commit** `feat(agents): evidence-gated suggestions`.

### Task 7: API routes

**Files:**
- Create: `src/trading/api/routes/agents.py`
- Modify: `src/trading/api/app.py` (include router), `tests/unit/api/test_openapi_snapshot.py` snapshot
- Test: `tests/integration/api/test_agents_api.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces (keep the existing `GET /v1/agents/runs` registered **before** `/v1/agents/{name}`):
  - `GET /v1/agents` (viewer) → list of `{name, health, last_run_at, enabled, uses_llm}`
  - `GET /v1/agents/{name}` (viewer) → `{info (registry), summary (24 h), config (effective values + which are overridden/pending), locked: [...]}`
  - `GET /v1/agents/{name}/decisions?limit=` (viewer) → `Decision[]`
  - `GET /v1/agents/{name}/suggestions` (viewer) → `Suggestion[]`
  - `GET /v1/agents/{name}/config-proposals` (viewer)
  - `POST /v1/agents/{name}/config-proposals` (operator, idempotent, body `{changes, rationale}`) → 201, 422 on `ProposalInvalid`
  - `POST /v1/agent-config-proposals/{id}/review` (operator, body `{approve: bool}`) → 200, 409 on `ProposalConflict`
- Unknown agent → 404 problem response, using `problems.py`.

- [ ] **Step 1: Failing tests:** viewer can read but gets 403 on POST; operator creates a proposal and approves it; locked field → 422; unknown agent → 404; `/v1/agents/runs` still works; empty agent returns 200 with idle health.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; regenerate the OpenAPI snapshot the way the existing test documents. **Step 4: Run** — PASS. **Step 5: Commit** `feat(api): agents endpoints`.

### Task 8: Dashboard view

**Files:**
- Modify: `src/trading/api/static/views.js` (add `agents` view), `app.js` (route + nav entry), `util.js` (icon `agents`), `style.css`
- Test: `tests/integration/api/test_dashboard_api.py` (extend)

**Interfaces:**
- Consumes: Task 7 routes. Layout follows `docs/superpowers/specs/assets/agent-panel-mockup.html`: agent tabs with health dot; numbers strip; Rules card; Config card (fields from `info.editable`, locked list, **Review change** opens the existing `openDialog` with a before/after diff and calls the review endpoint; operator-only controls hidden for viewers); decisions table with expandable row (evidence, limitations, hash, affected orders); suggestions card (`watching` vs `proposable`, with evidence `n / needed`, **Save as proposal** → POST with `source=SUGGESTION` behavior via the proposals endpoint).
- Every dynamic value through `el(...)`/`textContent`.

- [ ] **Step 1: Failing tests:** extend the static-asset test so the new view is registered and contains no `innerHTML|outerHTML|insertAdjacentHTML|document.write`; add a test that `index`/`app.js` references the `agents` route. (Assertions mirror the existing guard around line 151.)
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** the view using existing helpers (`el`, `append`, `badge`, `api`, `post`, `toast`). **Step 4: Run** tests — PASS; then **manually verify** in a browser at `http://127.0.0.1:8000/dashboard` (empty agent, rejected evaluator run, a macro run with decisions, an edit through the review dialog, viewer vs operator key). Report what was seen. **Step 5: Commit** `feat(dashboard): agents view`.

### Task 9: Docs and deploy check

**Files:**
- Modify: `README.md` (Dashboard section: Agents view, how edits apply), `docs/architecture/overview.md` (one paragraph), `docs/runbooks/startup-shutdown.md` (config reload exit code 4)

- [ ] **Step 1: Write the docs.** **Step 2: Run the whole test suite in chunks** (`tests/unit`, `tests/integration/agents`, `tests/integration/api`, `tests/integration/services`, `tests/replay`, rest) and check each **exit code**. **Step 3: Build the image** (`docker compose build worker api scheduler`), restart, confirm `/v1/agents` returns data and the worker is healthy. **Step 4: Commit** `docs: agent panel`.

---

## Self-review notes

- Spec coverage: numbers (T2, T7, T8), rules (T1), decisions/why (T2, T8), edit via reviewed proposals (T3–T5, T7, T8), session-boundary apply (T5), evidence-gated suggestions (T6, T8), locked fields (T1, T4, T7), untrusted text (T8). Prompt view-only (T1 `prompt_text`, shown in T8).
- Out of this plan by design: the evaluator `suggestions` schema fix and the quote-ingest lag (tracked separately).
- Type consistency: `AgentInfo.editable`, `Decision`, `Suggestion`, `propose_agent_config`/`review_agent_config`, `load_effective_config`, `EXIT_CONFIG_RELOAD` are named once and reused as written.
