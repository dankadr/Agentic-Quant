# 004 — dialog-lifecycle

- **Status**: DONE
- **Commit**: bf5b5a5
- **Scope**: src/trading/api/static/app.js: openDialog; style.css: dialog; focused browser regressions

## Problem and target

Add @starting-style dialog[open] opacity:0 transform:scale(0.96) translateY(6px). Existing 180ms opacity/transform cubic-bezier(0.23,1,0.32,1). Finish close on own opacity transitionend with >=200ms fallback, exactly once, guard repeated submission/cancel and cleanup listeners. Reduced motion immediate close, focus restored.

## Conventions and steps

Plain DOM ES modules, no dependencies. Existing --ease-out token is cubic-bezier(0.23,1,0.32,1). Read source mechanism before changes, write meaningful failing browser regression, implement exact target, rerun. Coordinate shared views.js/style.css with redesign owner.

## Boundaries

No financial/backend/auth changes. Preserve first-chart-only drawing and focused drafts. Report source drift before improvising.

## Verification

Run uv run pytest tests/e2e/test_dashboard_motion.py -q and existing dashboard tests. Browser feel check repeated interaction and interruptions in normal/reduced motion; inspect computed transforms and slow playback to ten percent for dialog/meter. Done when target behavior matches and regressions pass.

## Evidence

Focused Chromium regression suite passed all six cases, normal and reduced motion. All original five regressions observed RED before implementation (route harness corrected and RED rerun). Dialog entry keyframes verified starting scale0.96; close remains open mid-transition and restores trigger focus.
