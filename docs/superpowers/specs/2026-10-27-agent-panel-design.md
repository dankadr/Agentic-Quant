# Agent panel: dashboard view of each agent, with reviewed edits and evidence-gated suggestions

Date: 2026-10-27. Status: draft for approval. Builds on the deployed checkout (`/Users/dankadr/robinhood-trading`, branch lineage of `build/provider-cache`).

## Intent and the why

The owner is moving from paper trading toward real money and wants to *trust* the agents, not just run them.
On day one of live paper data the evaluator silently failed (reply rejected for an over-long `suggestions`
list), the macro agent returned `UNKNOWN` and halved entry sizes for ~1.5 h with no visible explanation, and
nobody could see which rules each agent follows. The dashboard already lists raw agent runs
(`/v1/agents/runs`), but not what an agent is for, what it may touch, why it decided what it decided, or how
to change its settings.

So: add an **Agents** view to the existing dashboard where, per agent, the owner can see (1) the numbers,
(2) the rules it follows, (3) why it made each decision and which trades it affected, (4) its settings, and
edit those settings through a reviewed, audited path, and (5) see suggestions for improving the settings that
are derived from past trades and always labelled with how much evidence supports them.

The target look is the approved mockup: one tab per agent (macro, fundamental, evaluator, consensus,
technical) with a health dot; a numbers strip; "Rules this agent follows"; a Config card with a diff-and-review
flow; a "Recent decisions, why" table with an expandable detail row; and an "AI suggestions" card.
Mockup: `docs/superpowers/specs/assets/agent-panel-mockup.html` (copied alongside this document).

## Goals

- Read-only transparency for every agent: runs, valid-reply rate, token use, latency, last run, output
  ranges, failures with their error text (e.g. the evaluator schema rejection).
- A plain-language "rules" card per agent: job, what it can affect, what it cannot do, model, prompt version,
  valid-output shape, failure behaviour. The prompt text is viewable.
- Per-decision "why": evidence, stated limitations, confidence, the multiplier or output it produced, the
  input snapshot/hash, and the orders/signals it demonstrably affected (e.g. macro multiplier on an entry's size).
- Editing of the numeric/boolean settings that already exist in `configs/agents.yaml` (enabled, intervals,
  ttl, `required`, lookback/headline limits, `dependent_strategies`), through proposals the owner approves.
- AI suggestions grounded in recorded trades and agent runs, each with an evidence count and a threshold
  below which it is shown only as "watching", never as a proposable change.

## Non-goals

- Editing an agent's **prompt** text (view only in v1; this is my call, see "Calls made").
- Editing risk limits, trading mode, broker, strategies' own parameters (those keep their existing proposal
  path via the technical agent), or anything that touches live trading.
- Letting an agent or an LLM apply any change by itself.
- Mobile layout, charts beyond simple sparklines, multi-user permissions beyond the existing viewer/operator roles.

## Anti-goals (a failure even if it technically works)

- Any path by which an agent suggestion changes behaviour without an explicit owner approval.
- Any edit that could loosen a risk control, switch mode, or enable margin/short/options/crypto. The Config
  card must show those as locked, and the API must refuse them.
- Suggestions presented as findings when they rest on a handful of trades. Weak evidence must look weak.
- Untrusted text (LLM output, news headlines) rendered as HTML. The dashboard's existing rule holds:
  `textContent` only, strict CSP, no external requests, and the existing test that guards against `innerHTML`.

## Constraints

- Safety invariants stay as they are: agents only propose; live mode is unrepresentable; settings validate
  through the existing pydantic models in `trading/core/config.py` (including cross-field rules such as
  macro `ttl_minutes > run_every_minutes`).
- Config today is read from `configs/agents.yaml`, baked into the Docker image. The panel needs a
  runtime-editable source without rebuilding the image. Approved edits must take effect **only at a session
  boundary** (same rule as `apply_approved` for strategy parameters), never mid-session.
- Read endpoints require the viewer role; any write requires the operator role, is idempotent, and is audited
  via the existing audit mechanism.
- Static, framework-free dashboard (`api/static/*.js`), same style as the existing views (`views.js`).
- Local LLM (self-hosted); cost fields are 0, so cost is not a metric to rely on; token counts are.
- Postgres tests run via testcontainers and are slow; new logic should be unit-testable without a database
  where practical.

## Decided with the owner

- Scope: per-agent info and numbers, the rules the agent follows, why it did what it did, and editing.
- Suggestions are based on past trades and carry visible evidence strength.
- Edits are reviewed before they apply (the mockup's "Review change" diff). Approved by the owner on seeing the mockup.

## Calls made by me (flag any you disagree with)

1. Prompt is view-only in v1. Prompt changes alter model behaviour in ways the schema cannot guard; they can be a later step with versioning.
2. Edits are stored as **proposals** (before/after diff, who, when, evidence) in a new table, mirroring
   `strategy_config_proposals`; approval marks them; they are applied at the next session start and the
   effective config is recorded with the run (`agent_runs` already stores model and prompt version).
3. Evidence thresholds are config, defaulting conservatively (for example, no proposable suggestion below
   10 trading days or 30 decisions). Below that, suggestions render as "watching".
4. The evaluator and technical agents are the natural producers of suggestions; the panel does not add a
   new LLM agent. Suggestion generation reuses recorded data; any LLM-written rationale is displayed as
   untrusted text.
5. Review is a single step: an operator proposes a change, sees the before/after diff, and approves it in the same flow. There is one owner today, so there is no separate second approver; the approval is audited, and the schema leaves room to require a different approver later.
6. Operators may flip `enabled` for any agent from the panel. Enabling an agent that needs the LLM key or news source shows a warning when that prerequisite is missing, since the job would skip anyway. The technical agent stays CLI-triggered (`trading technical`); the panel only shows its proposals and its `enabled` flag.

## Left to the builder

- Table/column names and migration details; how the effective config is resolved at session start
  (merge of YAML defaults and approved overrides) and how running agent jobs pick it up.
- API route shapes (suggested: `GET /v1/agents`, `GET /v1/agents/{name}`, `GET /v1/agents/{name}/decisions`,
  `POST /v1/agents/{name}/config-proposals`, review endpoints) following existing route conventions.
- How "which trades this decision affected" is joined (macro multiplier -> order sizing record, consensus ->
  signals, fundamental -> watchlist), using data already persisted.
- Test strategy beyond: schema/validation of edits, refusal of locked fields, session-boundary application,
  and the no-`innerHTML` guard.

## Out of scope for this document

The evaluator fix (tolerating an over-long `suggestions` list) and the remaining launch checks are tracked
separately and do not wait on this panel.
