# Agent upgrade implementation plan

Approved specification: conversation of 2026-11-14 (Hebrew proposed_plan).

Goal: reliable measurement, all five agents scheduled, three advisory agents, and controlled two-entry paper experiment under the existing ten-entry account cap.

Architecture: deterministic metrics and ranking; model explanations never manufacture numbers. Durable calendar jobs, lightweight research lane separate from heavy scratch research. Existing proposals and approval boundaries remain.

## Task 1: measurement
Correct percentages (keep fractional wire fields), decompose account equity change vs realized/unrealized/capital flows and attribution residual; slippage informational, no double deduction. Deterministic rendered evaluator facts with model explanations referencing metric IDs. Round trips, not partial fills, in performance and consensus. Tests: fractions, deposit, partial fills, repeated entries, causal consensus.

## Task 2: reentry
Add max_entries_per_symbol_per_day (default 1) and reentry_cooldown_minutes (default 30); active proposal 2/30. Durable counts of entry episodes plus last full exit, partial fills count once, pending orders block, zero-fill cancellations do not consume; maintain existing risk/global cap 10, sizing, exits. Track non-entry reasons and no-signal separately. Tests boundary, third entry, restart, cutoff, risk, partial fills.

## Task 3: advisory agents
Data-quality (5-minute during session, 30-minute window, coverage <95%, existing quote age), execution-quality (EOD costs/status/denials), opportunities (30-minute after open+30, top5 existing universe, equal percentile rank absolute session price change and recent5 volume/previous5-window average; valid data only; no profitability claim). Deterministic outputs into agent_runs. Registry/config/API/UI support, actual skip reasons and calendar-aware health. No execution authority.

## Task 4: scheduling and heavy research
Weekly 30-session collect/merge, train10/test5/holdout5 untouched; technical after experiments, proposal gates preserved. Dedicated heavy worker + scratch checks + single-flight; no model key/data means visible skipped run. Daily repeat-entry vs original 1-entry paired replay, separate scratch account; record trades/fees/slippage/drawdown, data/config fingerprints and causal windows. LLM transient failures at most2 retries, no retry400, sanitized category/reason. All jobs deduped.

## Task 5: integrate/verify/rollout
Main 3bc0dab base; original dirty worktree preserved. Preserve operating config ($100k, $12k trade size) rather than old worktree $8k/$6k changes. No risk increase. Additive OpenAPI fields, eight agents in panel. Tests/unit+integration full suite, ruff/mypy, mocked model end-to-end, final review. No production deployment or main merge without review; provide launch instructions, stage reentry config proposals at next session. Ten-session retrospective, no minimum-trade quota.

## Implementation verification

Implemented on feature/agent-upgrade, base3bc0dab. Measurement/reentry/advisory/heavy workflows and review fixes committed. Original dirty workspace and production stack preserved.

- Core replay/guards plus unit suite: 692 passed.
- Replay rejections:6 passed; strategy replay:7 passed; browser:15 passed.
- Chaos/restart cases:3 passed in initial broad run.
- Broad run completed all integration cases before replay partitioning. Two fixture assertions (synthetic funding and pre-upgrade parameter presence) were corrected; full quant API and proposal modules passed on rerun. No remaining known failing case.
- Ruff check/format:clean; mypy181 modules:clean; Compose config/whitespace:clean.
- Read-only operating-data check: 2026-11-13 net -54.1438 reconciles realized -189.8835 and unrealized +135.7397; residual0. SPY display -0.7526%.

Implementation defaults/limits: retain active $12k sizing and $100k funding; research requires35 bars and1 quote per traded symbol/session (not proof of data adequacy); ledger ADJUSTMENT is external capital and accounting corrections require classification review. Ambiguous recorded job/snapshot association skips paired comparison. No production deployment, merge or strategy activation performed. See docs/runbooks/agent-upgrade.md.
