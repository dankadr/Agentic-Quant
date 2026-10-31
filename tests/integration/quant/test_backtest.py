import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.core.config import load_config
from trading.portfolio.ledger import open_paper_account
from trading.quant.backtest import BacktestError, ensure_scratch, reset_scratch, run_backtest


@pytest.fixture
def make_principal(session_factory: sessionmaker[Session]):  # type: ignore[no-untyped-def]
    from trading.api.auth import Role, create_principal

    def _make() -> None:
        with session_factory.begin() as s:
            create_principal(s, name="op", role=Role.OPERATOR, now=T0)

    return _make


ROOT = Path(__file__).resolve().parents[3]
CFG = load_config(Path("configs"))
MON, TUE = date(2026, 10, 19), date(2026, 10, 20)


def _fixtures(tmp_path: Path, days: list[date], symbols: list[str]) -> Path:
    spec = importlib.util.spec_from_file_location(
        "synth", ROOT / "scripts" / "make_synthetic_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    bars, quotes = gen.generate_range(days, symbols, 7)
    out = tmp_path / "fx"
    out.mkdir()
    (out / "bars.csv").write_text(bars)
    (out / "quotes.csv").write_text(quotes)
    return out


def test_scratch_guard_refuses_a_database_with_a_real_account(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    open_paper_account(db, name="paper-primary", starting_cash=Decimal("1000"), now=T0)
    db.commit()
    with pytest.raises(BacktestError, match="scratch"):
        ensure_scratch(session_factory)
    with pytest.raises(BacktestError, match="scratch"):
        reset_scratch(session_factory, now=T0)
    assert db.execute(text("select count(*) from accounts")).scalar_one() == 1  # nothing was wiped


def test_scratch_guard_requires_a_scratch_looking_database_name(
    session_factory: sessionmaker[Session], db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I1: absence of accounts is not proof of a scratch DB (a fresh real one has none)."""
    monkeypatch.setattr("trading.quant.backtest._database_name", lambda session: "trading")
    with pytest.raises(BacktestError, match="scratch"):
        ensure_scratch(session_factory)
    with pytest.raises(BacktestError, match="scratch"):
        reset_scratch(session_factory, now=T0)


def test_scratch_guard_refuses_a_database_with_api_principals(
    session_factory: sessionmaker[Session], db: Session, make_principal
) -> None:  # type: ignore[no-untyped-def]
    make_principal()
    with pytest.raises(BacktestError, match="scratch"):
        reset_scratch(session_factory, now=T0)
    assert db.execute(text("select count(*) from principals")).scalar_one() == 1  # nothing wiped


def test_reset_clears_backtest_data_and_restores_the_fail_closed_seed(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    open_paper_account(db, name="backtest", starting_cash=Decimal("1000"), now=T0)
    db.commit()
    reset_scratch(session_factory, now=T0)
    db.expire_all()
    assert db.execute(text("select count(*) from accounts")).scalar_one() == 0
    assert (
        db.execute(text("select trading_state from system_state")).scalar_one() != "PAPER_RUNNING"
    )


def test_a_multi_day_backtest_keeps_one_account_and_carries_equity_forward(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    fx = _fixtures(tmp_path, [MON, TUE], ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA"])
    result = run_backtest(
        session_factory,
        fixtures_dir=fx,
        start=date(2026, 10, 17),  # Saturday: skipped, not an error
        end=TUE,
        config=CFG,
        minutes_per_day=40,
    )
    assert result.sessions == [MON, TUE]
    assert len(result.daily_returns) == 2
    assert result.account.scope == "account"
    assert set(result.strategies) >= {"orb_v1", "vwap_v1"}
    assert db.execute(text("select count(*) from accounts")).scalar_one() == 1
    assert db.execute(text("select count(*) from trading_sessions")).scalar_one() == 2
    snaps = db.execute(
        text(
            "select kind, equity from equity_snapshots"
            " where kind in ('SESSION_START', 'SESSION_END') order by ts, kind desc"
        )
    ).all()
    assert [k for k, _ in snaps] == ["SESSION_START", "SESSION_END"] * 2
    assert snaps[2].equity == snaps[1].equity  # day two starts from day one's close
    assert result.final_equity == snaps[3].equity
    assert result.daily_returns == tuple(
        (snaps[i + 1].equity - snaps[i].equity) / snaps[i].equity for i in (0, 2)
    )
    assert (
        db.execute(text("select count(*) from leases where expires_at > now()")).scalar_one() == 0
    )


def test_a_backtest_is_deterministic(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    fx = _fixtures(tmp_path, [MON, TUE], ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA"])
    kwargs = {"fixtures_dir": fx, "start": MON, "end": TUE, "config": CFG, "minutes_per_day": 30}
    first = run_backtest(session_factory, **kwargs)  # type: ignore[arg-type]
    second = run_backtest(session_factory, **kwargs)  # type: ignore[arg-type]
    assert first.result_hash == second.result_hash
    assert first.daily_returns == second.daily_returns


def test_an_empty_range_is_an_error(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    fx = _fixtures(tmp_path, [MON], ["SPY"])
    with pytest.raises(BacktestError, match="no trading sessions"):
        run_backtest(
            session_factory,
            fixtures_dir=fx,
            start=date(2026, 10, 17),
            end=date(2026, 10, 18),
            config=CFG,
        )


def test_backtest_evaluator_feeds_research_with_real_runs(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    from trading.quant.backtest import backtest_evaluator
    from trading.quant.research import sensitivity

    fx = _fixtures(tmp_path, [MON, TUE], ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA"])
    evaluator = backtest_evaluator(
        session_factory,
        fixtures_dir=fx,
        config=CFG,
        strategy_id="orb_v1",
        minutes_per_day=30,
    )
    result = sensitivity(evaluator, [MON, TUE], {"target_r": "1.5"}, {"target_r": ["1.0", "2.0"]})
    assert result.base.dates == (MON, TUE)
    assert result.base.params["target_r"] == "1.5"
    assert len(result.neighbors) == 2
    assert len(result.base.daily_returns) == 2
    # the override really reached the strategy row used by the run
    stored = db.execute(text("select params_json from strategies where id = 'orb_v1'")).scalar_one()
    assert stored["target_r"] == "2.0"  # the last evaluation ran with the last neighbour
    assert stored["trade_notional"] == "120"  # unrelated parameters are kept


def test_backtest_full_run_returns_one_return_per_session(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    from trading.quant.backtest import backtest_full_run

    fx = _fixtures(tmp_path, [MON, TUE], ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA"])
    full_run = backtest_full_run(
        session_factory,
        fixtures_dir=fx,
        config=CFG,
        strategy_id="orb_v1",
        dates=[MON, TUE],
        minutes_per_day=30,
    )
    by_day = full_run({"target_r": "2.0"})
    assert sorted(by_day) == [MON, TUE]
    assert all(isinstance(v, Decimal) for v in by_day.values())
    stored = db.execute(
        text("select params_json->>'target_r' from strategies where id = 'orb_v1'")
    ).scalar_one()
    assert stored == "2.0"  # the override reached the strategy used by the run
