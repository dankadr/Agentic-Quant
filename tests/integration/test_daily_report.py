import importlib.util
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from trading.core.config import load_config
from trading.monitoring.report import daily_report
from trading.quant.backtest import run_backtest

ROOT = Path(__file__).resolve().parents[2]
CFG = load_config(Path("configs"))
MON = date(2026, 10, 19)


@pytest.fixture
def fixtures(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "synth", ROOT / "scripts" / "make_synthetic_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    symbols = [u.symbol for u in CFG.universe if u.symbol in gen.START_CENTS]
    bars, quotes = gen.generate_range([MON], symbols, 7)
    out = tmp_path / "fx"
    out.mkdir()
    (out / "bars.csv").write_text(bars)
    (out / "quotes.csv").write_text(quotes)
    return out


def test_daily_report_summarises_data_coverage_trading_and_pnl(
    session_factory: sessionmaker[Session], db: Session, fixtures: Path
) -> None:
    run_backtest(
        session_factory, fixtures_dir=fixtures, start=MON, end=MON, config=CFG, minutes_per_day=60
    )
    db.expire_all()
    report = daily_report(db, MON, config=CFG, provider="replay")
    assert report["date"] == "2026-10-19"
    assert report["ticks"] == 60
    cov = report["data_coverage"]
    assert set(cov) == {u.symbol for u in CFG.universe}
    assert cov["SPY"] == {"bars": 60, "expected": 60, "missing": 0, "flagged": 0}
    # symbols without synthetic start prices have no data at all
    assert cov["TQQQ"]["bars"] == 60  # generator knows TQQQ since the multi-day fixtures were added
    assert report["equity"]["start"] is not None
    assert report["equity"]["end"] is not None
    assert set(report["strategies"]) >= {"orb_v1", "vwap_v1"}
    assert report["orders"]["total"] == sum(report["orders"]["by_status"].values())
    assert report["risk_denials"] == {} or all(
        isinstance(v, int) for v in report["risk_denials"].values()
    )
    assert report["costs"]["fees"] is not None
    assert report["benchmark"]["symbol"] == "SPY"
