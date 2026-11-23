# Exclusive ticker ownership release

Base: deployed local main `8d4141d` (15 commits ahead of GitHub main `19664f5`).
User authorization: push, merge, and deploy the ownership feature to the paper service.

## Behavior

- Reserve a ticker for one trading strategy from accepted entry until flat and all orders terminal.
- Apply the rule to both long and short entries; preserve owner exits, covers and safety flattening.
- Resolve simultaneous entries deterministically by strategy id (ORB before VWAP).
- Reconstruct ownership from account-scoped applied fills and unresolved orders, across restarts.
- Adjust historical fill quantities for applied splits before finding the current position cycle.
- Include completed unattributed fills in flat detection; exclude in-progress unattributed fills
  while the ledger is computing pre-fill short-owner P&L.
- Retain legacy shared positions for unwinding, while blocking additional entries.
- Show `SYMBOL_OWNED` diagnostics for reserved tickers.

## Validation and deployment

- Unit suite: 725 passed.
- Initial targeted ownership/allocator/execution/split/replay suite: 150 passed.
- Final portfolio/ownership/source/short-action regressions: 44 passed.
- Independent review found a final-cover attribution issue; regression reproduced and fixed.
- Full GitHub CI must pass before merge/deployment. Formatting-only fixes in the previously
  deployed base are included so the existing lint gate passes.
- Deploy a revision-tagged image using the existing paper-short risk override and persistent
  host mounts. Take a database backup first, stop worker/scheduler during the swap, then verify
  readiness, release identity, reconciliation, risk state and ownership on the active account.
- No ownership schema migration or risk-profile change is required.
