# Short positions in paper trading

Status: proposed for user review. No implementation or deployment authorized by this document alone.

## Intent and current behavior

The user wants agents to be able to short. The existing system is PAPER_ONLY and long-only. Agent outputs are advisory; deterministic strategies, allocation, risk checks and the paper broker decide and execute trades. Keep this authority model.

Observed blockers include `RiskPolicy.allow_short: Literal[False]`, SELL-as-exit assumptions in risk and allocation, venue capacity checks against held shares, ledger oversell rejection, a nonnegative positions constraint, and long-only flatten/reconciliation queries. Changing a flag alone is insufficient.

## Proposed scope (assistant defaults, awaiting user review)

Support simulated intraday shorts for the currently supported equities and ETFs. Use the existing supported-symbol universe. No live broker orders or actual borrowing are introduced. Shortability is a configured simulation assumption, displayed as such; this does not claim a real broker could locate stock. Retain existing strategy/agent enablement settings.

Enable bearish entries for the existing entry strategies using explicit, deterministic mirrored entry rules and short stops above entry. Agents continue to provide research and regime inputs; they do not obtain direct order authority. Preserve the current strategy's indicators, session limits and entry frequency. Exact symmetric strategy formulas must be documented in the implementation plan before coding. Do not treat every existing SELL exit as a short entry.

Ship support behind a default-off `allow_short` feature flag. Enabling it on the running paper stack is a separate rollout step after tests and user review of results. Existing longs retain their behavior with the flag off.

Turning the flag off while shorts exist blocks new/increased shorts but permits covers, reconciliation and flattening. Opposite-direction entries by another strategy in a symbol already held or reserved in the other direction are rejected; they do not close or borrow attribution from that strategy's position.

## Execution and accounting

Distinguish increase-long, reduce-long, increase-short, and reduce-short using an explicit position effect in signals and order intents. Broker BUY/SELL remains the execution side. Persist the effect through retries, reservations and fills; do not infer intent solely from a position that can change during a partial fill.

Store signed position quantities with nonnegative order/fill quantities. A sell opens/increases a short; a buy covers it. Disallow crossing directly through zero: close one direction, confirm fills and release reservations, then allow a new opposite entry. Preserve idempotency, serialization and fencing.

Opening short proceeds increase accounting cash but are restricted and do not expand buying power. Reserve collateral equal to 100% of short entry notional, in addition to restricting the proceeds, using available pre-existing capital. Revalue short liabilities and collateral at current conservative prices; pending entry orders also consume capacity. This is a fully collateralized paper simulation, not a model of broker margin rules. Any equity deficit stops entries and requests risk-reducing exits with fresh tradable data.

Specifically, collateral for the remaining short is `max(remaining entry basis, abs(quantity) * current ask)`. Restricted sale proceeds track remaining entry proceeds. On a partial cover, release entry basis and restricted proceeds in proportion to covered quantity and recompute collateral for the remainder. Free capital is settled cash less restricted proceeds, collateral and other pending-order reservations. If it becomes negative, reject all risk-increasing entries, cancel pending entries and request covers under existing data/session checks; covering may consume the short's own restricted funds. Record a collateral-deficit incident even if equity remains positive. Keep accounting equity separate from spendable capital.

Realized short profit is `(entry price - cover price) * covered quantity`, less fees. Mark short liabilities using ask quotes, and include signed market value in equity and absolute market value in gross exposure. Attribution, closed episodes, replay and reporting must agree. Splits adjust signed quantities and basis; dividend obligations on shorts must be debited if a short survives overnight, rather than silently crediting cash.

## Risk and exit behavior

Apply the existing total, symbol and sector exposure limits to absolute exposure in both directions. Shorts share the existing entry-count and open-symbol limits. Respect pause, disabled strategies, stale data, entry cutoff, loss and drawdown halts for new short entries.

Covering is a risk-reducing exit, like selling a long. Allow covers under the same halt/cutoff rules as existing risk-reducing exits, while retaining market-data and reconciliation requirements. Cover cannot exceed the remaining short after other pending covers. Prevent duplicate long sells and short covers from opening unintended opposite positions.

Flatten both directions before the close, cancel pending entries, and handle partial fills and restart recovery. If no executable quote exists, retain the position and report an incident; never fabricate a fill. A carried short remains visible and reserved across sessions. Borrow fees and locate availability are not simulated in this first version and must be disclosed in paper performance views.

## Dashboard

Show LONG/SHORT direction, absolute size, entry, short liability, unrealized/realized P&L and restricted collateral consistently. Distinguish SELL to open from SELL to close, and BUY to cover from BUY to open. Display the paper-only borrow assumptions and the short feature flag. Follow existing visual conventions.

## Validation and rollout

Test open/increase/partial cover/full cover, rejected crossing through zero, fees, cash restrictions, simultaneous/pending orders, insufficient collateral, short-disabled behavior, adverse price moves, halted-entry/allowed-cover behavior, end-of-day cover, split/dividend handling, reconciliation and restart. Run integration/replay cases through strategy → allocation → risk → broker → ledger → API. Retain long-only regression coverage and migrate existing positions without changing their quantities or basis.

Compare deterministic falling-price and rising-price replay scenarios with expected ledger/equity results. Test default-off behavior before any running-stack enablement. No automatic test triggers real trades or modifies production positions.

## Separate operational findings

On 2026-11-21, the container services recovered after a Docker Desktop restart and readiness passed. The latest agent history still includes failed macro/evaluator runs and stale RUNNING fundamental runs. Container health does not establish successful agent behavior. These are separate operational issues, not part of the short implementation, and must be reported honestly.

## Builder decisions

The builder chooses internal type names, reservation storage and migration mechanics, consistent with the invariants above. Preserve the newer deployed application features when planning integration: this chat's checkout differs from the deployed checkout at `/Users/dankadr/robinhood-trading`. Do not deploy this older checkout over the running stack. Use an isolated branch/worktree for implementation and preserve current uncommitted user changes.
