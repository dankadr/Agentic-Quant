# SYNTHETIC replay fixture — not market data

Seeded random-walk one-minute bars and quotes for SPY, QQQ and AAPL on 2026-10-19 (a regular
session, 13:30–20:00 UTC). Regenerate with:

    uv run python scripts/make_synthetic_fixtures.py --date 2026-10-19 --symbols SPY,QQQ,AAPL --seed 7 --out tests/fixtures/replay/2026-10-19

`tests/unit/data/test_synthetic_fixtures.py` fails if these files drift from the generator.
The sibling `gaps/` fixture drops SPY bars 14:00–14:04 UTC and stops all quotes at 15:00 UTC.
