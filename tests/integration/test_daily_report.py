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


def test_daily_report_reconciles_capital_realized_unrealized_and_attribution(db: Session) -> None:
    from datetime import timedelta
    from decimal import Decimal as D

    from tests.factories import T0
    from tests.integration.portfolio.helpers import open_account
    from trading.db.models.portfolio import CashLedgerEntry, EquitySnapshot

    acct = open_account(db)

    def snap(at, kind, equity, realized, unrealized):  # type: ignore[no-untyped-def]
        db.add(
            EquitySnapshot(
                account_id=acct,
                ts=at,
                kind=kind,
                cash=D(equity),
                settled_cash=D(equity),
                equity=D(equity),
                exposure=D(0),
                realized_pnl=D(realized),
                unrealized_pnl=D(unrealized),
                positions_json={},
            )
        )

    snap(T0, "SESSION_START", "1000", "10", "5")
    db.add(
        CashLedgerEntry(
            account_id=acct,
            ts=T0 + timedelta(minutes=10),
            amount=D("500"),
            currency="USD",
            source_type="ADJUSTMENT",
            source_id=None,
            settles_on=T0.date(),
            reverses_id=None,
        )
    )
    snap(T0 + timedelta(hours=6), "SESSION_END", "1503", "12", "6")
    db.flush()
    report = daily_report(db, T0.date(), config=CFG, provider="replay")
    e = report["equity"]
    assert D(e["equity_change"]) == D("503")
    assert D(e["capital_flows"]) == D("500")
    assert D(e["pnl"]) == D("3")
    assert D(e["realized_pnl_change"]) == D("2")
    assert D(e["unrealized_pnl_change"]) == D("1")
    assert D(e["reconciliation_residual"]) == 0
    assert D(e["attribution_residual"]) == D("2")
    assert e["return_pct_display"] == "0.3000%"
    assert report["costs"]["slippage_deducted_again"] is False


def test_diagnostics_count_sampled_no_entry_reasons_separately(db: Session) -> None:
    from tests.factories import T0, make_snapshot
    from tests.integration.portfolio.helpers import open_account
    from trading.db.models.research import AllocationDecision

    open_account(db)
    snapshot = make_snapshot(db)
    db.add(
        AllocationDecision(
            ts=T0,
            snapshot_id=snapshot.id,
            targets_json={},
            rationale_json=[
                {
                    "strategy_id": "orb_v1",
                    "symbol": "SPY",
                    "outcome": "NO_ENTRY",
                    "reason": "ENTRY_COOLDOWN",
                },
                {
                    "strategy_id": "orb_v1",
                    "symbol": "QQQ",
                    "outcome": "NO_ENTRY",
                    "reason": "NO_SIGNAL",
                },
                {"strategy_id": "orb_v1", "symbol": "MSFT", "outcome": "ALLOCATED", "reason": "OK"},
            ],
        )
    )
    db.flush()
    diagnostics = daily_report(db, T0.date(), config=CFG, provider="replay")["diagnostic_counts"]
    assert diagnostics["sampled_evaluations"] == 2
    assert diagnostics["by_strategy"] == {"orb_v1": {"ENTRY_COOLDOWN": 1, "NO_SIGNAL": 1}}
    assert "not unique opportunities" in diagnostics["note"]
