# Research reports: analysis, verification, digest

## Pipeline

1. A document version is stored (upload, approved URL import, or SEC discovery). Analysis is queued
   once per **content, model and prompt version** (`research_analyze:<version>:<model>:<prompt>`).
2. The research worker sends the stored page text to the model under a category-specific contract
   (`src/trading/reports/contracts.py`). The model must answer with one JSON object.
3. The answer is **verified in code** before anything is kept (`src/trading/reports/verify.py`):
   - every citation names a page that exists and a span inside it, and its quote must equal the
     stored text at that span;
   - every fact's value must parse as a number and appear in the quote it cites;
   - every SOURCE_FACT claim cites its source;
   - an AUTHOR_OPINION or MODEL_INTERPRETATION may not introduce a number that no fact supports.
   One failed check rejects the whole report. Partial acceptance is never done.
4. Outcomes are recorded, including refusals: `COMPLETED`, `REJECTED_INVALID` (with the violations),
   `FAILED` (model endpoint), `DEFERRED` (budget exhausted). Nothing is dropped silently.

## Availability and the digest

- `available_at` is set when a report is completed, and is the only time it may inform anything.
- `GET /v1/research/digest?as_of=...&kind=PREMARKET|EOD` shows only reports available by `as_of`,
  grouped by category, with their limitations and what was missing.

## Budgets

- `documents.daily_token_budget` caps tokens per UTC day across all reports. Exhaustion records
  `DEFERRED / BUDGET_EXHAUSTED`. Trading is never delayed by research.
- `documents.max_input_chars` caps one document's text. A longer document is refused with
  `CONTEXT_TOO_LARGE`, never silently truncated.

## Scope and limits

- **Shadow only.** Nothing in execution, risk, portfolio, broker or quant imports the research
  packages; an architecture test enforces this. No report alters an order, a risk limit or a
  trading state.
- **Discovery:** `sec_discover` lists recent 10-K, 10-Q and 8-K filings for a CIK, with the
  declared `documents.sec_user_agent` and courtesy spacing. It is queued on demand; scheduled
  30-minute polling is **not yet wired** into the scheduler.
- **Macro releases, transcripts and analyst reports** are analysed when supplied or imported. Their
  official release calendars are not yet polled.
- **Numbers:** facts store value, unit, period and accounting basis. Comparisons are only computed
  for matching company, key, unit and basis (`src/trading/reports/compare.py`).
- The model is a scripted fake in tests. Quality against real documents is **not yet evaluated**;
  that is Milestone 8's evaluation set.
