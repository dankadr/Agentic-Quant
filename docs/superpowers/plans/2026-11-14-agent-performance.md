# Agent Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Follow TDD and review before release.

**Goal:** Fix demonstrated exit reservations and make agent performance evidence interpretable.

**Architecture:** Existing fenced executor owns cancellations and fills. Existing AgentRun columns/JSON hold accurate telemetry. Read-only scorecards aggregate versioned evidence; no execution authority is added.

**Tech Stack:** Python3.12, SQLAlchemy/Postgres, FastAPI, DOM ES modules; no dependencies/migrations by default.

**Spec:** docs/superpowers/specs/2026-11-14-agent-performance-design.md

## Global Constraints

Paper-only; preserve risk, sizing, entry caps and proposal approval. Unknown cost stays NULL. Source output is untrusted. Legacy rows must not masquerade as current measured evidence. Keep the original dirty checkout and canonical private reports untouched.

## Review Focus

- Fill during cancel must reduce replacement quantity; timeout must not release reserved shares.
- Effective flatten configuration, manual flatten and repeated ticks must remain consistent.
- Legacy zero duration/cost and missing usage must be shown as unknown.
- Missing/failed/skipped/inflight runs must not inflate completion or trade-utility scores.
- Version changes, unmatched replay/configs and tiny samples must never imply causal improvement.

## Task 1: Repair end-of-day exit reservation

Files: execution/manager.py, execution/tick.py, appropriate execution/quant integration tests; alert/audit integration only if needed.

- [x] Reproduce unfilled acknowledged exit blocking flatten with real DB and paper broker.
- [x] Implement fenced cancel/sync/rebuild behavior at flatten boundary using effective strategy configuration.
- [x] Test partial fill during cancel, canceled/expired status, cancellation timeout, repeated tick and missing quote; assert no oversell and unresolved state is visible.
- [x] Run execution integration and targeted replay/control regressions; self-review and commit.

## Task 2: Accurate model run telemetry

Files: agents/fundamental.py,evaluator.py,technical.py,jobs.py; heavy caller; tests/integration/agents and focused unit tests.

- [x] Add failing tests that advance injected clock during LLM call, return usage and assert completion/availability, NULL cost and measured-version marker.
- [x] Extend run functions compatibly with injectable clock; forward runtime clocks, preserve usage and unknowns.
- [x] Cover failed/invalid outcomes and no-experiment path without invented usage.
- [x] Run focused model/job/technical suites; self-review and commit.

## Task 3: Scorecard design and implementation

- [x] Finalize presentation preference and precise read API semantics; log decision in spec.
- [x] Build read-only versioned completion/validation/observability card with raw denominators and evidence gates; add meaningful API/browser regressions.
- [x] Exclude legacy consensus utility from trading contribution and show no valid paired evidence until present.
- [x] Regenerate OpenAPI, verify desktop/mobile and existing route/security/accessibility flows.

## Release

- [x] Fresh independent code review of execution safety and metric denominators.
- [x] Ruff, formatting, mypy, relevant integration/browser/guard regressions; inspect every result.
- [ ] Push/deploy through established revision image process; authenticate and verify release/schema/eight agents, scorecards and healthy services.
