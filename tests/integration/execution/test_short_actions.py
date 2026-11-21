from datetime import timedelta

import pytest
from sqlalchemy import select, text

from tests.integration.execution.env import CFG, DAY, D, build_env
from tests.integration.execution.test_reconcile import run
from trading.core.enums import PositionEffect, Side
from trading.core.errors import TradingError
from trading.db.models.market import CorporateAction
from trading.db.models.portfolio import Position
from trading.jobs.lease import renew_lease
from trading.portfolio.corporate_actions import apply_due_actions
from trading.quant.holdings import load_holdings
from trading.quant.registry import seed_strategies
from trading.quant.roundtrips import load_roundtrips


def test_carried_short_split_dividend_and_cover(db, session_factory, clock, calendar):
    env = build_env(db, session_factory, clock, calendar, allow_short=True)
    with session_factory.begin() as s:
        seed_strategies(s, CFG, now=clock.now())
        assert renew_lease(s, env.lease, now=clock.now(), ttl=timedelta(days=2))
    env.tick()
    order = env.place(side=Side.SELL, effect=PositionEffect.OPEN_SHORT, qty="2", limit="98")
    env.manager.submit(order)
    assert env.status(order) == "ACKNOWLEDGED", env.row(order, "status_reason")
    env.tick()
    env.manager.sync_fills()
    before = env.broker.get_account()
    assert before.positions["SPY"] == -2
    tomorrow = DAY + timedelta(days=1)
    now = clock.now() + timedelta(days=1)
    clock.set(now)
    with session_factory.begin() as s:
        for kind, fields in (
            ("SPLIT", {"ratio_from": D(1), "ratio_to": D(2)}),
            ("CASH_DIVIDEND", {"cash_amount": D("0.5")}),
        ):
            s.add(
                CorporateAction(
                    instrument_id=env.instruments["SPY"].id,
                    ex_date=tomorrow,
                    action_type=kind,
                    provider="p",
                    announced_at=now,
                    ingested_at=now,
                    **fields,
                )
            )
    for _ in range(2):
        with session_factory.begin() as s:
            apply_due_actions(
                s,
                account_id=env.account_id,
                broker=env.broker,
                session_date=tomorrow,
                now=now,
                provider="p",
            )
    after = env.broker.get_account()
    assert after.positions["SPY"] == -4
    assert after.cash == before.cash - 2
    with session_factory() as s:
        assert s.scalar(select(Position.qty)) == -4
        (lot,) = load_holdings(s, env.account_id, now)
        assert lot.qty == -4
        assert lot.avg_cost == s.scalar(select(Position.average_cost))
    assert run(env).status == "OK"
    cover = env.place(side=Side.BUY, effect=PositionEffect.CLOSE_SHORT, qty="4", limit="51")
    env.manager.submit(cover)
    env.tick(bid="49.99", ask="50")
    env.manager.sync_fills()
    assert env.broker.get_account().positions == {}
    with session_factory() as s:
        assert s.scalar(select(Position.qty)) == 0
        (trip,) = load_roundtrips(s, env.account_id, end=clock.now() + timedelta(seconds=1))
        assert trip.pnl == s.scalar(select(Position.realized_pnl))
    assert run(env).status == "OK"


@pytest.mark.parametrize(("qty", "ratio_from"), [("1", "3"), ("0.0001", "2")])
def test_unsupported_reverse_split_leaves_both_books_and_marker_untouched(
    db, session_factory, clock, calendar, qty, ratio_from
):
    env = build_env(db, session_factory, clock, calendar, allow_short=True)
    env.tick()
    order = env.place(side=Side.SELL, effect=PositionEffect.OPEN_SHORT, qty=qty, limit="98")
    env.manager.submit(order)
    env.tick()
    env.manager.sync_fills()
    before = env.broker.get_account()
    assert before.positions["SPY"] == -D(qty)
    now = clock.now() + timedelta(days=1)
    tomorrow = DAY + timedelta(days=1)
    with session_factory.begin() as s:
        s.add(
            CorporateAction(
                instrument_id=env.instruments["SPY"].id,
                ex_date=tomorrow,
                action_type="SPLIT",
                ratio_from=D(ratio_from),
                ratio_to=D(1),
                provider="p",
                announced_at=now,
                ingested_at=now,
            )
        )
    with pytest.raises(TradingError, match="split"):
        with session_factory.begin() as s:
            apply_due_actions(
                s,
                account_id=env.account_id,
                broker=env.broker,
                session_date=tomorrow,
                now=now,
                provider="p",
            )
    assert env.broker.get_account() == before
    with session_factory() as s:
        assert s.scalar(select(Position.qty)) == -D(qty)
        assert s.scalar(text("SELECT count(*) FROM paper_venue.venue_actions")) == 0
        assert (
            s.scalar(
                text("SELECT count(*) FROM audit_events WHERE action='corporate_action.applied'")
            )
            == 0
        )


@pytest.mark.parametrize(
    ("entry", "first_action"),
    [
        (PositionEffect.OPEN_SHORT, "SPLIT"),
        (PositionEffect.OPEN_LONG, "SPLIT"),
        (PositionEffect.OPEN_SHORT, "CASH_DIVIDEND"),
    ],
)
def test_session_start_adjusts_marks_and_rejects_late_actions(
    db, session_factory, clock, calendar, entry, first_action
):
    from tests.integration.execution.env import REF
    from trading.db.models.market import MarketBar, MarketQuote
    from trading.db.models.portfolio import EquitySnapshot, TradingSession
    from trading.services.bootstrap import build_runtime

    env = build_env(db, session_factory, clock, calendar, allow_short=True)
    env.tick()
    order = env.place(
        side=entry.side, effect=entry, qty="2", limit="98" if entry.is_short else "102"
    )
    env.manager.submit(order)
    env.tick()
    env.manager.sync_fills()
    prior = clock.now()
    action_day = DAY + timedelta(days=2)
    with session_factory.begin() as s:
        assert renew_lease(s, env.lease, now=prior, ttl=timedelta(days=4))
        s.add(
            MarketBar(
                instrument_id=env.instruments["SPY"].id,
                ts=prior - timedelta(minutes=1),
                interval="1m",
                provider="p",
                open=D(100),
                high=D(100),
                low=D(100),
                close=D(100),
                volume=D(1000),
                ingested_at=prior,
                session_date=DAY,
                quality_flags=[],
            )
        )
        s.add(
            MarketQuote(
                instrument_id=env.instruments["SPY"].id,
                ts=prior,
                bid=D("99.99"),
                ask=D(100),
                provider="p",
                ingested_at=prior,
                quality_flags=[],
            )
        )
        s.add(
            CorporateAction(
                instrument_id=env.instruments["SPY"].id,
                action_type=first_action,
                ex_date=action_day,
                **(
                    {"ratio_from": D(1), "ratio_to": D(2)}
                    if first_action == "SPLIT"
                    else {"cash_amount": D(1)}
                ),
                provider="p",
                announced_at=prior,
                ingested_at=prior,
            )
        )
    runtime = build_runtime(
        session_factory=session_factory,
        config=CFG,
        clock=clock,
        lease=env.lease,
        calendar=calendar,
        provider=None,
        provider_name="p",
        account_name=REF,
    )
    clock.set(prior + timedelta(days=2))
    before = env.broker.get_account()
    runtime.processor.start_session(action_day)
    after = env.broker.get_account()
    expected = after.cash + before.positions["SPY"] * 100
    intraday_mark = D(100) if entry.is_short else D("99.995")
    intraday_equity = after.cash + before.positions["SPY"] * intraday_mark
    runtime.processor.run_tick(clock.now())
    with session_factory() as s:
        assert (
            s.scalar(
                select(TradingSession.start_equity).where(TradingSession.session_date == action_day)
            )
            == expected
        )
        assert (
            s.scalar(
                select(EquitySnapshot.equity)
                .where(EquitySnapshot.kind == "INTRADAY")
                .order_by(EquitySnapshot.ts.desc())
                .limit(1)
            )
            == intraday_equity
        )
    with session_factory.begin() as s:
        s.add(
            CorporateAction(
                instrument_id=env.instruments["SPY"].id,
                action_type="CASH_DIVIDEND" if first_action == "SPLIT" else "SPLIT",
                ex_date=DAY + timedelta(days=1),
                **(
                    {"cash_amount": D(1)}
                    if first_action == "SPLIT"
                    else {"ratio_from": D(1), "ratio_to": D(2)}
                ),
                provider="p",
                announced_at=prior,
                ingested_at=clock.now(),
            )
        )
    clock.advance(timedelta(minutes=1))
    runtime.processor.run_tick(clock.now())
    with session_factory() as s:
        assert (
            s.scalar(
                text(
                    "SELECT count(*) FROM incidents WHERE incident_key LIKE 'corporate_action:%' AND state='OPEN'"
                )
            )
            == 1
        )
        assert s.scalar(text("SELECT count(*) FROM paper_venue.venue_actions")) == 1
        assert (
            s.scalar(
                text("SELECT count(*) FROM audit_events WHERE action='corporate_action.applied'")
            )
            == 1
        )
    assert env.broker.get_account().cash == after.cash
