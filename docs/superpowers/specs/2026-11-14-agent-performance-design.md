# Agent reliability and performance evidence

The owner approved implementing the historical review's ordered fixes. Source review: operating private report data/reports/agent-history-2026-11-14/review.md in the canonical checkout. The first delivery fixes end-of-day exit reservation and records trustworthy model-run telemetry. It preserves paper-only mode, risk limits, account sizing, current entry caps, model choice and reviewed-proposal boundaries.

## Execution repair

A resting exit can reserve all shares and block end-of-day flatten as NO_HOLDING. Reproduce at the executor/broker boundary before changing code. During the effective flatten window, or an authorized manual FLATTENING state, cancel superseded outstanding exit orders through the existing fenced order manager, synchronize any fills, and rebuild remaining holdings before allocating the replacement. Unconfirmed cancellation must continue reserving shares; never bypass ownership or submit duplicate sells. Repeated ticks, partial fills while canceling, broker timeouts, expired orders and stale quotes must be safe. Use effective flatten strategy parameters, not a new hardcoded time. Surface unresolved positions in the existing end-session audit/readiness/alerts path.

## Model telemetry and availability

Fundamental, Evaluator and Technical currently record completion equal to the passed start time and an invented zero cost. Add injectable runtime clock support, set actual completion/availability, retain returned prompt/completion usage, keep unmetered cost NULL, and stamp explicit telemetry metadata so legacy zero-duration rows are never interpreted as measured latency. Pass the runtime clock from jobs/heavy callers. Do not fabricate token usage on failed calls that did not return usage. Macro already records real clock/token usage and unknown cost; preserve it. Historical HTTP400 subtype is unavailable; current safe allowlisted diagnostics remain the source of truth. Do not retry permanent400 blindly or claim an external provider repair without evidence.

## Proposed scorecard (presentation preference pending)

Separate reliability, output quality and trading contribution; no arbitrary overall rank. Display agent role, run/model/prompt version, reporting window, sample size and independent session count. Empty samples show insufficient evidence, not zero scores. Reliability exposes completion percentage and explicit failed/invalid/skipped/inflight counts; do not label completion as on-time reliability unless expected jobs and usable output timing are measured. Quality initially exposes validation outcomes and telemetry completeness, with role-specific factual/citation correctness explicitly unmeasured until audited. Contribution requires causal paired portfolio replay and matched data/config fingerprints; legacy consensus-v1 filtered-trade P&L must never become a performance score. No unproven alpha number, automatic parameter change or ranking across different agent roles. Keep raw numerators and denominators visible.

The scorecard implementation/design is finalized after the owner's presentation preference response; existing execution and telemetry repairs proceed independently.
