# Agent reliability and performance evidence

The owner approved implementing the historical review's ordered fixes. Source review: operating private report data/reports/agent-history-2026-11-14/review.md in the canonical checkout. The first delivery fixes end-of-day exit reservation and records trustworthy model-run telemetry. It preserves paper-only mode, risk limits, account sizing, current entry caps, model choice and reviewed-proposal boundaries.

## Execution repair

A resting exit can reserve all shares and block end-of-day flatten as NO_HOLDING. Reproduce at the executor/broker boundary before changing code. During the effective flatten window, or an authorized manual FLATTENING state, cancel superseded outstanding exit orders through the existing fenced order manager, synchronize any fills, and rebuild remaining holdings before allocating the replacement. Unconfirmed cancellation must continue reserving shares; never bypass ownership or submit duplicate sells. Repeated ticks, partial fills while canceling, broker timeouts, expired orders and stale quotes must be safe. Use effective flatten strategy parameters, not a new hardcoded time. Surface unresolved positions in the existing end-session audit/readiness/alerts path.

## Model telemetry and availability

Fundamental, Evaluator and Technical currently record completion equal to the passed start time and an invented zero cost. Add injectable runtime clock support, set actual completion/availability, retain returned prompt/completion usage, keep unmetered cost NULL, and stamp explicit telemetry metadata so legacy zero-duration rows are never interpreted as measured latency. Pass the runtime clock from jobs/heavy callers. Do not fabricate token usage on failed calls that did not return usage. Macro already records real clock/token usage and unknown cost; preserve it. Historical HTTP400 subtype is unavailable; current safe allowlisted diagnostics remain the source of truth. Do not retry permanent400 blindly or claim an external provider repair without evidence.

## Proposed scorecard (presentation preference pending)

Separate reliability, output quality and trading contribution; no arbitrary overall rank. Display agent role, run/model/prompt version, reporting window, sample size and independent session count. Empty samples show insufficient evidence, not zero scores. Reliability exposes completion percentage and explicit failed/invalid/skipped/inflight counts; do not label completion as on-time reliability unless expected jobs and usable output timing are measured. Quality initially exposes validation outcomes and telemetry completeness, with role-specific factual/citation correctness explicitly unmeasured until audited. Contribution requires causal paired portfolio replay and matched data/config fingerprints; legacy consensus-v1 filtered-trade P&L must never become a performance score. No unproven alpha number, automatic parameter change or ranking across different agent roles. Keep raw numerators and denominators visible.

The scorecard implementation/design is finalized after the owner's presentation preference response; existing execution and telemetry repairs proceed independently.

## Confirmed scorecard delivery

Owner selected separate dashboard measurement cards. Implement a read-only `/v1/agents/performance?days=30` endpoint (days1–90) and eight cards above the existing Agents detail panel. Current prompt version is prominent; historical cohorts are separately grouped by prompt version AND model, never pooled into a current score. Exclude future-start rows and treat future completion as in progress at report as-of.

For each cohort: recorded-run completion score = COMPLETED/(COMPLETED+FAILED+REJECTED_INVALID)*100, NULL if no terminal attempts. Skipped and running are separately visible; scheduled-job delivery/on-time percentage stays NULL until schedule/run linkage is recorded. Output acceptance = COMPLETED/(COMPLETED+REJECTED_INVALID)*100, explicitly a schema/domain outcome proxy, never factual accuracy; infrastructure failures excluded and visible. Factual quality stays unmeasured with a role-specific explanation. Trading contribution stays unscored: legacy consensus-v1 is excluded and current filtered-trade consensus is labelled observational, not portfolio replay. Existing1-vs2 strategy experiments cannot score individual agents.

Measured latency accepts positive durations from successful Macro rows with explicit availability/usage, or run-telemetry-v1 runtime-clock rows; old Fundamental/Evaluator/Technical zero-duration rows remain unknown. Token reporting counts known rows and sums only known usage; LLM zero-dollar fields without explicit metering remain unknown. Include distinct observed New York session dates, raw counts, missing telemetry, errors safely categorized (HTTP400, invalid_output, stale/insufficient inputs, other), as-of/window and no cross-role overall ranking. Eligibility limits are text and sample counts, not fabricated statistical confidence.

API errors show unavailable cards with retry. Polling preserves focused settings/drafts. Use plain text DOM, responsive existing palette and no additional motion/dependencies. Add deterministic aggregation, authentication/version/time-boundary regressions and browser card/unknown/error coverage.
