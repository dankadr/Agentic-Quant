from datetime import timedelta

from sqlalchemy import select

from tests.integration.execution.env import CFG, DAY, D, build_env
from tests.integration.execution.test_reconcile import run
from trading.core.enums import PositionEffect, Side
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
