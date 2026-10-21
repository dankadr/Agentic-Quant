from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from tests.integration.risk.helpers import DAY, NEXT, D, make_normal, pf_for
from tests.unit.risk.helpers import CONFIG, instrument, portfolio, symbol_view
from trading.control.state import (
    RiskState,
    TradingState,
    get_system_state,
    set_risk_state,
    set_trading_state,
)
from trading.core.enums import OrderType, Side, TimeInForce
from trading.data.snapshot import MarketSnapshot
from trading.db.engine import make_engine, make_session_factory
from trading.risk.engine import DeterministicRiskEngine
from trading.risk.halts import NotInHaltError, clear_drawdown_halt, evaluate_halts, start_session
from trading.risk.models import ProposedOrder
from trading.risk.service import build_account_view

POLICY = CONFIG.risk


def _halts(db: Session, account_id, equity: str, day=DAY, now=T0):  # type: ignore[no-untyped-def]
    return evaluate_halts(db, account_id=account_id, state=pf_for(account_id, equity), session_date=day,
                          policy=POLICY, now=now)  # fmt: skip


def _row(db: Session, sql: str):  # type: ignore[no-untyped-def]
    return db.execute(text(sql)).scalar_one()


def test_start_session_is_idempotent(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("555"), now=T0)
    assert _row(db, "select count(*) from trading_sessions") == 1
    assert _row(db, "select start_equity from trading_sessions") == D("1000.0000")


@pytest.mark.parametrize(("equity", "state"), [
    ("1000", RiskState.NORMAL),
    ("990.01", RiskState.NORMAL),
    ("990.00", RiskState.HALTED_DAILY_LOSS),  # a loss of exactly 1% halts (decision C4)
    ("960", RiskState.HALTED_DAILY_LOSS),  # 4% down: daily limit hit, drawdown (5%) not yet
    ("900", RiskState.HALTED_DRAWDOWN),  # 10% down: the drawdown halt takes precedence
])  # fmt: skip
def test_daily_loss_boundary(db: Session, equity: str, state: RiskState) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    assert _halts(db, account_id, equity) is state
    assert get_system_state(db).risk_state is state


def test_daily_halt_records_time_and_emits_alert(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    later = T0 + timedelta(minutes=5)
    _halts(db, account_id, "985", now=later)
    assert _row(db, "select daily_loss_halted_at from trading_sessions") == later
    kinds = (
        db.execute(text("select payload_json->>'kind' from outbox where event_type = 'risk_alert'"))
        .scalars()
        .all()
    )
    assert kinds == ["DAILY_LOSS"]


def test_halt_does_not_clear_when_equity_recovers(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    _halts(db, account_id, "985")
    assert _halts(db, account_id, "1020") is RiskState.HALTED_DAILY_LOSS


@pytest.mark.parametrize(("equity", "state"), [
    ("950.01", RiskState.HALTED_DAILY_LOSS),  # >1% daily loss but drawdown not yet 5%
    ("950.00", RiskState.HALTED_DRAWDOWN),  # exactly 5% from peak halts
])  # fmt: skip
def test_drawdown_boundary_and_precedence(db: Session, equity: str, state: RiskState) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    assert _halts(db, account_id, equity) is state


def test_peak_equity_ratchets_up_and_drives_drawdown(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    assert _halts(db, account_id, "1100") is RiskState.NORMAL
    assert _row(db, "select peak_equity from accounts") == D("1100.0000")
    assert _halts(db, account_id, "1045") is RiskState.HALTED_DRAWDOWN  # exactly 5% below 1100
    assert _row(db, "select peak_equity from accounts") == D("1100.0000")


def test_halts_never_override_unknown_or_reconciliation(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    for state in (RiskState.RECONCILIATION_REQUIRED, RiskState.UNKNOWN):
        set_risk_state(db, risk_state=state, reason="x", actor="t", now=T0)
        assert _halts(db, account_id, "500") is state
        assert get_system_state(db).risk_state is state


def test_missing_session_row_fails_closed_to_unknown(db: Session) -> None:
    account_id = make_normal(db)
    assert _halts(db, account_id, "1000") is RiskState.UNKNOWN
    assert get_system_state(db).risk_state is RiskState.UNKNOWN


def test_missing_session_row_keeps_a_drawdown_halt(db: Session) -> None:
    account_id = make_normal(db)
    set_risk_state(db, risk_state=RiskState.HALTED_DRAWDOWN, reason="dd", actor="t", now=T0)
    assert _halts(db, account_id, "1000") is RiskState.HALTED_DRAWDOWN


def test_daily_halt_clears_at_next_session_but_not_on_same_day_restart(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    _halts(db, account_id, "985")
    start_session(
        db, account_id=account_id, session_date=DAY, start_equity=D("985"), now=T0
    )  # restart
    assert get_system_state(db).risk_state is RiskState.HALTED_DAILY_LOSS
    start_session(
        db,
        account_id=account_id,
        session_date=NEXT,
        start_equity=D("985"),
        now=T0 + timedelta(days=1),
    )
    assert get_system_state(db).risk_state is RiskState.NORMAL
    reason = get_system_state(db).risk_reason
    assert reason == "new_session"


def test_drawdown_halt_survives_new_session_and_needs_operator(db: Session) -> None:
    account_id = make_normal(db)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
    _halts(db, account_id, "940")
    start_session(
        db,
        account_id=account_id,
        session_date=NEXT,
        start_equity=D("940"),
        now=T0 + timedelta(days=1),
    )
    assert get_system_state(db).risk_state is RiskState.HALTED_DRAWDOWN
    # the latest equity snapshot is what the peak resets to
    from trading.portfolio.valuation import record_equity_snapshot

    record_equity_snapshot(db, pf_for(account_id, "940"), kind="INTRADAY")
    state = clear_drawdown_halt(
        db, actor="alice", reason="reviewed", request_id="req-1", now=T0 + timedelta(days=1)
    )
    assert state.risk_state is RiskState.NORMAL
    assert _row(db, "select peak_equity from accounts") == D("940.0000")
    audit = db.execute(
        text(
            "select actor, request_id, details_json->>'reason' from audit_events where action = 'risk.clear_drawdown_halt'"
        )
    ).one()
    assert tuple(audit) == ("alice", "req-1", "reviewed")


def test_clear_drawdown_halt_requires_that_state(db: Session) -> None:
    make_normal(db)
    with pytest.raises(NotInHaltError):
        clear_drawdown_halt(db, actor="alice", reason="x", request_id=None, now=T0)


def test_daily_halt_persists_across_restart(
    database_url: str, session_factory: sessionmaker[Session], db: Session, calendar
) -> None:  # type: ignore[no-untyped-def]
    """Acceptance test 5: a daily-loss halt survives a worker restart; exits stay allowed."""
    with session_factory.begin() as s:
        account_id = make_normal(s)
        set_trading_state(s, target=TradingState.PAPER_RUNNING, expected_version=get_system_state(s).version,
                          actor="op", reason="go", request_id=None, now=T0)  # fmt: skip
        start_session(s, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=T0)
        assert _halts(s, account_id, "985") is RiskState.HALTED_DAILY_LOSS

    # "restart": a brand-new engine, pool and session factory
    restarted = make_session_factory(make_engine(database_url))
    with restarted.begin() as s:
        assert get_system_state(s).risk_state is RiskState.HALTED_DAILY_LOSS
        start_session(
            s, account_id=account_id, session_date=DAY, start_equity=D("985"), now=T0
        )  # same-day restart
        held = pf_for(account_id, "985", positions=[("SPY", "1", "100")])
        view = build_account_view(
            s, account_id=account_id, portfolio=held, session_date=DAY, config=_config()
        )
        market = MarketSnapshot(id=uuid4(), as_of=T0.replace(hour=15, minute=0), provider="p",
                                symbols={"SPY": symbol_view("SPY", now=T0.replace(hour=15, minute=0))},
                                quality_status="OK", data_hash="h")  # fmt: skip
        engine = DeterministicRiskEngine(
            POLICY, calendar=calendar, paper_cfg=CONFIG.paper_broker, limits_version="v"
        )
        now = T0.replace(hour=15, minute=0)

        def proposal(side: Side, limit: str) -> ProposedOrder:
            return ProposedOrder(account_id=account_id, instrument=instrument("SPY"), side=side, quantity=D("0.5"),
                                 order_type=OrderType.LIMIT, limit_price=D(limit), time_in_force=TimeInForce.DAY,
                                 strategy_id="orb_v1", signal_id=None, signal_expires_at=None, dedupe_key=f"k-{side}")  # fmt: skip

        buy = engine.evaluate(proposal(Side.BUY, "100.00"), view, market, now=now)
        sell = engine.evaluate(proposal(Side.SELL, "99.00"), view, market, now=now)
        assert [r.code.value for r in buy.reasons if r.code.value == "DAILY_LOSS_LIMIT"] == [
            "DAILY_LOSS_LIMIT"
        ]
        assert not buy.allowed
        assert sell.allowed, [r.code for r in sell.reasons]
    restarted.kw["bind"].dispose()


def _config():  # type: ignore[no-untyped-def]
    return CONFIG


def test_portfolio_helper_roundtrip() -> None:
    assert portfolio(equity="10").equity == D("10")


def test_clear_drawdown_halt_cli(
    database_url: str, session_factory: sessionmaker[Session], db: Session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    runner = CliRunner()
    assert runner.invoke(app, ["admin", "clear-drawdown-halt", "--reason", "x"]).exit_code == 1
    with session_factory.begin() as s:
        make_normal(s)
        set_risk_state(s, risk_state=RiskState.HALTED_DRAWDOWN, reason="dd", actor="t", now=T0)
    result = runner.invoke(app, ["admin", "clear-drawdown-halt", "--reason", "reviewed"])
    assert result.exit_code == 0, result.output
    assert '"risk_state": "NORMAL"' in result.stdout
    db.rollback()
    assert get_system_state(db).risk_state is RiskState.NORMAL
