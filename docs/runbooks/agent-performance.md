# Agent performance evidence

The Agents dashboard includes eight separate cards. GET `/v1/agents/performance?days=30`
(viewer authentication; days1–90) returns the same evidence.

## Meaning of the numbers

- Recorded-run completion is completed / (completed + failed + rejected-invalid), expressed as
  a percentage. Skips and in-progress runs are visible separately. It does not measure missed
  scheduled jobs or on-time delivery.
- Output acceptance is a schema/domain validation proxy for model agents. Factual correctness,
  citation fidelity and economic usefulness are explicitly unmeasured.
- Trading contribution is unscored until an agent-specific causal paired portfolio comparison
  exists. Old consensus-v1 figures and strategy1-vs2 entry experiments do not score agent alpha.
- Each cohort separates prompt version and model. The configured current cohort is prominent;
  other versions/models are expandable and never pooled into its score. Configuration matching
  is not inferred. Small samples show counts and observed calendar dates in Eastern time,
  rather than statistical certainty or an overall league table.
- Missing evidence produces no numerical score. Legacy synthetic zero durations and unmetered
  zero-dollar model costs remain unknown. Usage is known only when the provider reported it;
  incomplete usage across schema-repair requests remains unknown.

The current cards are a baseline for improvement, not an autonomous tuning loop. Subsequent
quality evaluation should use independent source/incident labels: factual source support for
Fundamental, deterministic numeric checks for Evaluator, labelled detection/diagnosis cases for
Data/Execution Quality, and untouched out-of-sample research for Technical. Macro and any
execution-affecting policy require a matched causal portfolio replay with net returns,
drawdown, costs and session counts. Ranking activity is not ranking expected returns.

## Execution repair

During the effective flatten window, or a valid manual flatten request, the strategy executor
cancels older reserved exits and rebuilds remaining quantity through the existing allocator
and risk engine. New exits created in the current minute survive repeated ticks, independent
of which original strategy owns the shares. A cancellation is made terminal only when the
broker's cumulative filled quantity matches imported fills; delayed feeds retain the reservation.
Missing/stale quotes, ambiguous broker state or delayed fills can still leave residual holdings.
Existing session-end audit records those quantities; cancellation is not a promise of execution.

## Release and observation

Migration0012 allows the heavy service's PAPER_COMPARISON and RESEARCH_BATCH experiment kinds
and SKIPPED status. Unknown values remain rejected. Downgrade refuses newer history rather
than deleting it. Application and migration builds share the same revision arguments/image.

Before rollout, back up the operating DB and verify the dump; migrate using the new revision's
owner-role migration service, then start the services. Migrate the dedicated scratch DB with
its own restricted owner credentials. Never use tests or scratch resets on the operating DB.
Confirm revision/schema, healthy services, eight cards and current/historical version isolation.

Review the first complete sessions for residual positions, exit replacement churn, stale Macro
coverage, safe endpoint error subtypes, real completion times and usage completeness. Preserve
risk limits and entry caps; further changes use the existing reviewed-proposal workflow.
