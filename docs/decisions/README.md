# Architecture decision log

| ID | Decision | Source |
|----|----------|--------|
| C1–C10 | Spec conflicts and their resolutions (sleeve semantics, fractional shares, risk-before-intent, boundary semantics, halts vs exits, halt clearing, UNKNOWN orders, expired intents, account IDs, config split) | [roadmap §1](../superpowers/plans/2026-10-20-roadmap-and-spec-review.md) |

Rulings made while building Plan A (settlement of debits vs credits, claim-fenced job completion,
fail-closed reconciliation, idempotency claim-first, deterministic replay hashing, and others) were
recorded as the work progressed; they are summarized in the Plan A completion report.
