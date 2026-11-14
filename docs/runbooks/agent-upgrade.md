# Agent upgrade rollout

Implementation branch: feature/agent-upgrade. No production stack is changed by checking it out.

## Prepare

1. Back up the operating database and preserve its stored strategy parameters and applied agent settings.
2. Provision and migrate a **dedicated disposable database** named `trading_agent_scratch`. Give the heavy worker credentials for that database through `TRADING_SCRATCH_DATABASE_URL`. It must not be the registry database. Scratch resets refuse accounts/API principals unrelated to backtests. Never point this URL at an operating database.
3. Build a revision-tagged image using the existing release process. Deploy measurement/advisory changes first. The Compose `heavy` service stores collected sessions/cache under the persistent `data/agent-research` bind mount, runs one heavy job at a time, and claims no trading or intraday model jobs.
4. Check `/v1/agents`: eight entries, enabled state from effective settings, expected schedule, last successful run and skip reason. Previously approved agent settings override YAML, so review any existing applied override that disables technical or a new agent. Technical waits until Saturday 08:00 UTC, then collects 30 closed sessions and runs train10/test5/holdout5 research without evaluating holdout. Missing scratch/model/data is visible as SKIPPED.

## Activate repeat entries at the next session boundary

YAML and old stored parameters remain at one entry by default. For **each** of `orb_v1` and `vwap_v1`, read the current strategy version from `/v1/strategies`, then POST to `/v1/strategies/{id}/propose-config` using the existing operator authentication and idempotency header:

```json
{
  "base_version": "USE_CURRENT_VERSION",
  "parameters": {
    "max_entries_per_symbol_per_day": 2,
    "reentry_cooldown_minutes": 30
  },
  "rationale": "Approved paper experiment: bounded repeat entries, account cap stays 10"
}
```

Use a different operator to approve through the existing `/v1/config-proposals/{id}/approve` workflow. Confirm the new version applies at session start. This does not increase the ten-entry account cap, entry size, exposure, loss limits or change exits. Roll back by proposing limit 1 with the same workflow.

## Observe

Data quality runs every five minutes; opportunity activity ranking every thirty minutes after open+30; execution quality after close+13. These agents are advisory and cannot trade. Existing fundamental news output is untrusted research with citations, not verified fact. Opportunities rank activity, not expected profitability.

Paired comparison runs after close+15 on recorded data. The scheduler captures effective strategy configuration before enqueueing, so backlog cannot silently change its parameters. Both arms use fresh scratch accounts, common starting equity, identical input and causal macro history; only entry limit 1 versus 2 differs. Observed intraday snapshot timestamps supply the replay tick timeline, preserving normal scheduling jitter; missing timeline produces an explicit skip. It is not a reconstruction of operating fills or overnight positions, and preserves recorded ingestion times and quality flags. Results are in `experiments` under `PAPER_COMPARISON`, with data/config fingerprints, P&L after fees, informational slippage, roundtrip counts and maximum drawdown. `review_due` marks the ten-session review point and includes normalized paired-return statistics. Compare daily returns when funding differs; do not add dollar P&L across differently funded periods.

Review sampled NO_ENTRY reasons, invalid quotes, missing data, agent errors and both replay arms after ten sessions. More transactions alone is not a success criterion. Use existing pause/stop controls for execution faults. No strategy suggestion or review result applies itself.

## Accounting interpretation

Wire percentage fields retain fractional units; display fields label percentages. Attribution is realized performance, while account equity includes unrealized changes. Reconciliation exposes unexplained residuals. Ledger INITIAL_DEPOSIT and ADJUSTMENT entries (and their reversals) are external capital; dividends are performance. Classify any accounting correction before treating an adjustment as a deposit. Slippage is already reflected in simulated execution prices and is never deducted a second time.

Research prerequisite floor: every traded symbol must have at least 35 bars and one quote on each
of the thirty sessions. Below that the technical workflow records an insufficient-coverage skip.
This floor is prerequisite availability, not a claim that sparse IEX data is adequate for inference.

## Dashboard verification

The overview uses the selected session's ending equity, net performance and realized/unrealized
changes. Current exposure and positions are intentionally unavailable when inspecting an explicit
historical date. Agent health is current; run timestamps are independently labelled, and the
latest-per-agent query prevents frequently running agents from hiding daily agents. The API
supports `/v1/agents/runs?latest_per_agent=true&date=YYYY-MM-DD`; the date is optional and uses UTC
run-start boundaries. Missing requests show unavailable sections with retry, rather than zero
performance or a healthy system. Equity charts offer keyboard inspection and a data table.

For a dashboard release, run the dashboard API integration modules and browser UI/motion tests.
Inspect desktop and phone layouts in both themes. Confirm no external requests, no script execution
from agent text, focused drafts survive refresh, and reduced motion removes positional feedback.
After rollout, authenticate with a temporary viewer principal, check revision/schema and the eight
configured agents, request a daily report and latest-per-agent results, then disable the principal.
Enabled agents can be idle outside their schedules; that is distinct from a successful job run.
