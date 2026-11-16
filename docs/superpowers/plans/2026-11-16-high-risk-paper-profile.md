# High-risk paper profile deployment plan

**Goal:** Apply the owner-approved higher entry frequency and larger paper trades.
**Scope:** Configuration only; retain the existing strategies, capital sleeves, exits, quality checks, and paper-only constraints.
**Approval:** Owner approved the complete parameter table and entry-rule changes in the 2026-11-16 conversation.

- [x] Update existing configuration assertions for the approved values; observe failures against the current files.
- [x] Change risk limits: symbol 20%, sector 40%, daily loss 5%, drawdown 10%, 20 new entries per day, 8 open symbols. Keep gross exposure 80% and cash reserve 20%.
- [x] Change both strategies: target notional $20,000, 2 entries per symbol per day, 15-minute re-entry cooldown. ORB opening range 10 minutes, volume ratio 1.0. VWAP lookback 20, minimum bars 25, entry z 1.5, slope limit 12. Preserve every other setting.
- [x] Validate configuration and run configuration/strategy unit tests and proposal-session integration tests on a disposable database.
- [x] Record strategy proposals with Codex as preparer and the actual owner approval as reviewer; preserve next-session activation. Never fabricate a second human review.
- [ ] Commit only the reviewed deployment files, build a revision-tagged image, restart application services, and audit the risk activation.
- [ ] Verify loaded risk settings in every application container, executor health, and approved proposal values in the database. Record that strategy settings activate on 2026-11-17 session start.

## Rollback
The previous configuration files, revision, and a consistent pre-change database dump are saved outside the repository. Redeploy revision 19664f5 for risk rollback. Before next-session activation, reject/cancel the two pending approvals through an explicitly audited operator action; do not restore the database over ongoing trading data.

Pre-deployment verification passed: configuration/ORB/VWAP unit tests, proposal/session/backtest integration tests on disposable PostgreSQL, Ruff checks, and git diff whitespace checks. Both live proposals are APPROVED with unchanged base versions 1.0.1; application remains deferred to the next real session. Deployment results are recorded in the external backup directory.
