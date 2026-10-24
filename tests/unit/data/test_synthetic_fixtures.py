import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "replay"


def _generator():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "synth", ROOT / "scripts" / "make_synthetic_fixtures.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_generator_is_deterministic() -> None:
    gen = _generator()
    a = gen.generate(date(2026, 10, 19), ["SPY", "QQQ", "AAPL"], 7)
    b = gen.generate(date(2026, 10, 19), ["SPY", "QQQ", "AAPL"], 7)
    c = gen.generate(date(2026, 10, 19), ["SPY", "QQQ", "AAPL"], 8)
    assert a == b
    assert a != c


def test_committed_fixtures_match_generator() -> None:
    gen = _generator()
    bars, quotes = gen.generate(date(2026, 10, 19), ["SPY", "QQQ", "AAPL"], 7)
    assert (FIXTURES / "2026-10-19" / "bars.csv").read_text() == bars
    assert (FIXTURES / "2026-10-19" / "quotes.csv").read_text() == quotes
    gbars, gquotes = gen.generate(
        date(2026, 10, 19), ["SPY", "QQQ", "AAPL"], 7, ["SPY:14:00-14:05"], "15:00"
    )
    assert (FIXTURES / "gaps" / "bars.csv").read_text() == gbars
    assert (FIXTURES / "gaps" / "quotes.csv").read_text() == gquotes


def test_fixtures_are_labelled_synthetic() -> None:
    for name in ("2026-10-19", "gaps"):
        for kind in ("bars.csv", "quotes.csv"):
            assert (FIXTURES / name / kind).read_text().startswith("# SYNTHETIC")


def test_range_generator_concatenates_sessions_under_one_header() -> None:
    gen = _generator()
    days = [date(2026, 10, 19), date(2026, 10, 20)]
    bars, quotes = gen.generate_range(days, ["SPY"], 7)
    assert bars == gen.generate_range(days, ["SPY"], 7)[0]
    assert bars.count("symbol,ts,open") == 1
    assert quotes.count("symbol,ts,bid") == 1
    assert "2026-10-19T13:30:00Z" in bars
    assert "2026-10-20T13:30:00Z" in bars
    first_day, _ = gen.generate(days[0], ["SPY"], 7)
    assert all(line in bars for line in first_day.splitlines() if line.startswith("SPY,"))
    assert bars.count("\nSPY,") == 2 * 390
