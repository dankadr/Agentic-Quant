from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.factories import T0, make_snapshot
from tests.integration.portfolio.helpers import get_instrument, new_order, open_account
from tests.integration.risk.helpers import DAY, D, make_normal, pf_for
from tests.unit.risk.helpers import CONFIG, instrument, symbol_view
from trading.control.state import RiskState, TradingState
from trading.core.enums import OrderType, Side, TimeInForce
from trading.core.errors import TradingError
from trading.data.snapshot import MarketSnapshot
from trading.portfolio.balances import MissingMark
from trading.risk.engine import DeterministicRiskEngine
from trading.risk.models import AccountView, ProposedOrder
from trading.risk.service import build_account_view, evaluate_and_record


def _engine(calendar):  # type: ignore[no-untyped-def]
    return DeterministicRiskEngine(
        CONFIG.risk,
        calendar=calendar,
        paper_cfg=CONFIG.paper_broker,
        limits_version=CONFIG.limits_version,
    )


def _proposal(
    account_id, instr, *, qty="1", limit="100.00", dedupe="k1", strategy="orb_v1", side=Side.BUY
):  # type: ignore[no-untyped-def]
    return ProposedOrder(account_id=account_id, instrument=instr, side=side, quantity=D(qty),
                         order_type=OrderType.LIMIT, limit_price=D(limit), time_in_force=TimeInForce.DAY,
                         strategy_id=strategy, signal_id=None, signal_expires_at=None, dedupe_key=dedupe)  # fmt: skip


def _market(db: Session, now):  # type: ignore[no-untyped-def]
    row = make_snapshot(db, now=now, data_hash=f"m-{now.isoformat()}")
    return MarketSnapshot(id=row.id, as_of=now, provider="p", symbols={"SPY": symbol_view("SPY", now=now)},
                          quality_status="OK", data_hash="h")  # fmt: skip


NOW = T0.replace(hour=15, minute=0)


def _running_view(db: Session, account_id, portfolio=None):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    view = build_account_view(db, account_id=account_id, portfolio=portfolio or pf_for(account_id, "1000"),
                              session_date=DAY, config=CONFIG)  # fmt: skip
    return replace(view, trading_state=TradingState.PAPER_RUNNING)


def test_decision_recorded_with_reasons_and_traceability(db: Session, calendar) -> None:  # type: ignore[no-untyped-def]
    account_id = make_normal(db)
    instr = get_instrument(db, "SPY")
    info = instrument("SPY", id=instr.id)  # type: ignore[arg-type]
    market = _market(db, NOW)
    allowed = evaluate_and_record(db, _engine(calendar), proposal=_proposal(account_id, info), account=_running_view(db, account_id),
                                  market=market, portfolio_snapshot_id=None, now=NOW)  # fmt: skip
    denied = evaluate_and_record(db, _engine(calendar), proposal=_proposal(account_id, info, qty="5", dedupe="k2"),
                                 account=_running_view(db, account_id), market=market, portfolio_snapshot_id=None, now=NOW)  # fmt: skip
    assert allowed.evaluation.allowed
    assert not denied.evaluation.allowed
    rows = {str(r.id): r for r in db.execute(text("select * from risk_decisions")).all()}
    a, d = rows[str(allowed.decision_id)], rows[str(denied.decision_id)]
    assert (a.allowed, a.reasons_json, a.limits_version, a.market_snapshot_id) == (
        True,
        [],
        CONFIG.limits_version,
        market.id,
    )
    assert d.allowed is False
    assert d.reasons_json == [
        {"code": "EXPOSURE_LIMIT", "detail": "symbol"},
        {"code": "EXPOSURE_LIMIT", "detail": "sector"},
    ]
    assert a.proposal_json["strategy_id"] == "orb_v1"
    assert a.proposal_json["quantity"] == "1"
    assert a.dedupe_key == "k1"


def test_recorded_decisions_are_immutable(db: Session, calendar) -> None:  # type: ignore[no-untyped-def]
    account_id = make_normal(db)
    info = instrument("SPY", id=get_instrument(db, "SPY").id)  # type: ignore[arg-type]
    rec = evaluate_and_record(db, _engine(calendar), proposal=_proposal(account_id, info), account=_running_view(db, account_id),
                              market=_market(db, NOW), portfolio_snapshot_id=None, now=NOW)  # fmt: skip
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text("update risk_decisions set allowed = false where id = :i"), {"i": rec.decision_id}
        )


def test_second_evaluation_of_an_allowed_key_is_a_recorded_duplicate_not_an_error(
    db: Session, calendar
) -> None:  # type: ignore[no-untyped-def]
    account_id = make_normal(db)
    info = instrument("SPY", id=get_instrument(db, "SPY").id)  # type: ignore[arg-type]
    market = _market(db, NOW)
    first = evaluate_and_record(db, _engine(calendar), proposal=_proposal(account_id, info), account=_running_view(db, account_id),
                                market=market, portfolio_snapshot_id=None, now=NOW)  # fmt: skip
    second = evaluate_and_record(db, _engine(calendar), proposal=_proposal(account_id, info),
                                 account=_running_view(db, account_id), market=market, portfolio_snapshot_id=None, now=NOW)  # fmt: skip
    assert first.evaluation.allowed
    assert [r.code.value for r in second.evaluation.reasons] == ["DUPLICATE_INTENT"]
    assert (
        db.execute(text("select count(*) from risk_decisions where dedupe_key = 'k1'")).scalar_one()
        == 2
    )


def test_build_account_view_reads_authoritative_state(db: Session) -> None:
    account_id = make_normal(db)
    new_order(db, account_id, "b-open", qty="2", limit="100.00")  # reserves 200 on SPY
    new_order(db, account_id, "b-aapl", qty="1", limit="50.00", symbol="AAPL")
    new_order(db, account_id, "s-open", side="SELL", qty="0.7", limit="90.00")
    new_order(db, account_id, "b-done", qty="9", limit="100.00", status="FILLED")
    new_order(db, account_id, "b-rejected", qty="9", limit="100.00", status="REJECTED")
    db.execute(text("insert into trading_sessions(account_id, session_date, start_equity, new_entries) values (:a, :d, 1000, 3)"),
               {"a": account_id, "d": DAY})  # fmt: skip
    view = build_account_view(
        db,
        account_id=account_id,
        portfolio=pf_for(account_id, "1000"),
        session_date=DAY,
        config=CONFIG,
    )
    assert dict(view.open_buy_notional_by_symbol) == {"SPY": D("200.0000"), "AAPL": D("50.0000")}
    assert dict(view.open_buy_notional_by_sector) == {
        "Broad Market": D("200.0000"),
        "Technology": D("50.0000"),
    }
    assert dict(view.open_sell_qty_by_symbol) == {"SPY": D("0.7")}
    assert view.new_entries_today == 3
    assert view.risk_state is RiskState.NORMAL
    assert view.trading_state is TradingState.PAUSED
    assert "orb_v1" in view.enabled_strategies
    assert (
        not {"fundamental_watchlist", "consensus_shadow", "cash_reserve"} & view.enabled_strategies
    )


def test_new_entries_default_zero_without_session_row(db: Session) -> None:
    account_id = make_normal(db)
    assert (
        build_account_view(
            db, account_id=account_id, portfolio=None, session_date=DAY, config=CONFIG
        ).new_entries_today
        == 0
    )


def test_partially_filled_buy_counts_only_remaining(db: Session) -> None:
    from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill
    from trading.portfolio.ledger import apply_fill

    account_id = make_normal(db)
    order = new_order(db, account_id, "b1", qty="2", limit="100.00")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(order, "e1", qty="0.5", price="99"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    view = build_account_view(
        db, account_id=account_id, portfolio=None, session_date=DAY, config=CONFIG
    )
    assert dict(view.open_buy_notional_by_symbol) == {"SPY": D("150.0000")}


def test_market_buy_needs_an_ask_to_value(db: Session) -> None:
    account_id = make_normal(db)
    new_order(db, account_id, "m1", qty="1", limit=None)
    with pytest.raises(MissingMark):
        build_account_view(
            db, account_id=account_id, portfolio=None, session_date=DAY, config=CONFIG
        )
    view = build_account_view(db, account_id=account_id, portfolio=None, session_date=DAY, config=CONFIG,
                              ask_by_symbol={"SPY": D("101")})  # fmt: skip
    assert dict(view.open_buy_notional_by_symbol) == {"SPY": D("101.0000")}


def test_types_exported() -> None:
    assert issubclass(MissingMark, TradingError)
    assert AccountView.__dataclass_fields__["portfolio"]
    assert timedelta(seconds=1)
    assert open_account
