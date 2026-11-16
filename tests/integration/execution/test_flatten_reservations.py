from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.integration.execution.env import CFG, Env, build_env
from trading.core.enums import Side
from trading.execution.state_machine import OrderStatus


@pytest.fixture
def env(db, session_factory, clock, calendar):
    return build_env(db, session_factory, clock, calendar)


def hold(env: Env) -> None:
    from trading.quant.registry import seed_strategies

    with env.sf.begin() as session:
        seed_strategies(session, CFG, now=env.clock.now())
    buy = env.place(qty="8", limit="101")
    env.manager.submit(buy)
    env.tick()
    env.manager.sync_fills()


def test_cancel_imports_partial_fill_before_releasing_reservation(env: Env, monkeypatch) -> None:
    hold(env)
    sell = env.place(side=Side.SELL, qty="8", limit="101")
    env.manager.submit(sell)
    original = env.spy.cancel_order

    def fill_then_cancel(client_id):
        env.tick(bid="102", ask="102.02", volume="100")
        return original(client_id)

    monkeypatch.setattr(env.spy, "cancel_order", fill_then_cancel)
    assert env.manager.cancel(sell, reason="FLATTEN_REPLACE") is OrderStatus.CANCELED
    assert env.scalar("select qty from positions") == 3
    assert env.row(sell).filled_qty == 5
    assert env.manager.sync_fills() == 0


def processor(env: Env):
    from trading.execution.tick import PaperTickProcessor
    from trading.quant.registry import seed_strategies
    from trading.quant.source import StrategyIntentSource
    from trading.risk.engine import DeterministicRiskEngine

    with env.sf.begin() as s:
        seed_strategies(s, CFG, now=env.clock.now())
        s.execute(text("update leases set expires_at = expires_at + interval '1 day'"))
        s.execute(
            text(
                "update strategies set params_json = '{\"flatten_minutes_before_close\": 20}' where id = 'eod_flatten'"
            )
        )
    source = StrategyIntentSource(
        env.sf,
        account_id=env.account_id,
        calendar=env.calendar,
        config=CFG,
        instruments=env.instruments,
        provider_name="p",
    )
    return PaperTickProcessor(
        env.sf,
        account_id=env.account_id,
        broker=env.broker,
        manager=env.manager,
        engine=DeterministicRiskEngine(
            CFG.risk, calendar=env.calendar, paper_cfg=CFG.paper_broker, limits_version="test"
        ),
        provider=None,
        provider_name="p",
        intents=source,
        instruments=env.instruments,
        calendar=env.calendar,
        config=CFG,
        clock=env.clock,
        lease=env.lease,
    )


def market(env: Env, *, quote=True):
    from trading.db.models.market import MarketBar, MarketQuote

    now = env.clock.now()
    with env.sf.begin() as s:
        s.add(
            MarketBar(
                instrument_id=env.instruments["SPY"].id,
                interval="1m",
                ts=now - timedelta(minutes=1),
                provider="p",
                open=100,
                high=101,
                low=99,
                close=100,
                volume=100000,
                ingested_at=now,
                session_date=now.date(),
                quality_flags=[],
            )
        )
        if quote:
            s.add(
                MarketQuote(
                    instrument_id=env.instruments["SPY"].id,
                    ts=now,
                    provider="p",
                    bid=Decimal("99.99"),
                    ask=100,
                    ingested_at=now,
                    quality_flags=[],
                )
            )


def test_effective_flatten_window_replaces_reserved_exit_once(env: Env):
    hold(env)
    sell = env.place(side=Side.SELL, qty="8", limit="110")
    env.manager.submit(sell)
    tick = processor(env)
    env.clock.set(env.clock.now().replace(hour=19, minute=40, second=0))
    market(env)
    result = tick.run_tick(env.clock.now())
    assert env.status(sell) == "CANCELED"
    assert result.orders_submitted == 1
    assert tick.run_tick(env.clock.now()).orders_submitted == 0
    assert (
        env.scalar("select count(*) from orders where side='SELL' and status='ACKNOWLEDGED'") == 1
    )
    env.clock.advance(timedelta(minutes=1))
    market(env)
    tick.run_tick(env.clock.now())
    assert env.scalar("select qty from positions") == 0


@pytest.mark.parametrize(
    "outcome",
    [
        "partial",
        "filled",
        "timeout",
        "timeout_partial",
        "pending",
        "expired",
        "missing_quote",
        "stale_quote",
        "manual",
        "before_window",
    ],
)
def test_flatten_cancellation_outcomes(env: Env, monkeypatch, outcome):
    from trading.brokers.base import BrokerTimeout

    hold(env)
    sell = env.place(side=Side.SELL, qty="8", limit="110")
    env.manager.submit(sell)
    tick = processor(env)
    hour, minute = (18, 0) if outcome in {"manual", "before_window"} else (19, 40)
    env.clock.set(env.clock.now().replace(hour=hour, minute=minute, second=0))
    if outcome == "manual":
        with env.sf.begin() as s:
            s.execute(text("update system_state set trading_state='FLATTENING'"))
    market(env, quote=outcome != "missing_quote")
    if outcome == "stale_quote":
        with env.sf.begin() as s:
            s.execute(text("update market_quotes set ts=ts-interval '10 minutes'"))
    original = env.spy.cancel_order

    def cancel(client_id):
        if outcome == "timeout_partial":
            env.tick(bid="111", ask="111.02", volume="100", advance=0)
        if outcome in {"timeout", "timeout_partial"}:
            raise BrokerTimeout("pending cancellation")
        if outcome == "pending":
            return env.broker.get_order(client_id)
        if outcome in {"partial", "filled"}:
            env.tick(
                bid="111",
                ask="111.02",
                volume="100" if outcome == "partial" else "100000",
                advance=0,
            )
        if outcome == "expired":
            env.broker.expire_day_orders(session_close=env.clock.now())
            return env.broker.get_order(client_id)
        return original(client_id)

    monkeypatch.setattr(env.spy, "cancel_order", cancel)
    result = tick.run_tick(env.clock.now())
    blocked = outcome in {
        "timeout",
        "timeout_partial",
        "pending",
        "missing_quote",
        "stale_quote",
        "before_window",
        "filled",
    }
    assert result.orders_submitted == (0 if blocked else 1)
    if outcome in {"timeout", "pending", "before_window"}:
        assert env.status(sell) == "ACKNOWLEDGED"
    if outcome == "timeout_partial":
        assert env.row(sell).filled_qty == 5
        assert env.status(sell) == "PARTIALLY_FILLED"
        assert env.scalar("select qty from positions") == 3
    if outcome == "expired":
        assert env.status(sell) == "EXPIRED"
    if outcome == "partial":
        assert env.row(sell).filled_qty == 5
        assert env.scalar("select qty from orders where side='SELL' and status='ACKNOWLEDGED'") == 3
    if blocked and outcome != "filled":
        env.clock.set(env.clock.now().replace(hour=20, minute=5))
        tick.end_session(env.clock.now().date())
        assert env.scalar("select details_json from audit_events where action='session.end'")[
            "unresolved_positions"
        ]["SPY"] == ("3" if outcome == "timeout_partial" else "8")


def test_flatten_cancellation_still_requires_executor_fence(env: Env):
    from trading.jobs.lease import LeaseLost

    hold(env)
    sell = env.place(side=Side.SELL, qty="8", limit="110")
    env.manager.submit(sell)
    with env.sf.begin() as s:
        s.execute(text("update leases set fencing_token=fencing_token+1"))
    with pytest.raises(LeaseLost):
        env.manager.cancel_flatten_exits(account_id=env.account_id, minute=env.clock.now())
    assert env.status(sell) == "ACKNOWLEDGED"
    assert env.broker.get_order(env.row(sell).client_order_id).status.value == "ACCEPTED"


def test_repeated_tick_preserves_unfilled_strategy_owned_flatten_order(env: Env, monkeypatch):
    hold(env)
    old_exit = env.place(side=Side.SELL, qty="8", limit="110")
    env.manager.submit(old_exit)
    tick = processor(env)
    env.clock.set(env.clock.now().replace(hour=19, minute=40, second=0))
    market(env)
    # Keep the replacement unfilled, so the second tick exercises preservation rather than a fill.
    monkeypatch.setattr(env.broker, "process_tick", lambda **kwargs: [])
    cancellations = []
    original = env.spy.cancel_order

    def cancel(client_id):
        cancellations.append(client_id)
        return original(client_id)

    monkeypatch.setattr(env.spy, "cancel_order", cancel)
    assert tick.run_tick(env.clock.now()).orders_submitted == 1
    replacement = env.scalar("select id from orders where side='SELL' and status='ACKNOWLEDGED'")
    assert env.scalar(
        "select strategy_ids from order_intents order by created_at desc limit 1"
    ) == ["orb_v1"]
    tick.run_tick(env.clock.now())
    assert env.status(replacement) == "ACKNOWLEDGED"
    assert len(cancellations) == 1


def test_cancel_waits_for_delayed_fill_feed_before_releasing_reservation(env: Env, monkeypatch):
    hold(env)
    sell = env.place(side=Side.SELL, qty="8", limit="101")
    env.manager.submit(sell)
    original_cancel = env.spy.cancel_order
    original_fills = env.broker.list_fills

    def fill_then_cancel(client_id):
        env.tick(bid="102", ask="102.02", volume="100", advance=1)
        return original_cancel(client_id)

    monkeypatch.setattr(env.spy, "cancel_order", fill_then_cancel)
    monkeypatch.setattr(env.broker, "list_fills", lambda **kwargs: [])
    assert env.manager.cancel(sell, reason="FLATTEN_REPLACE") is OrderStatus.ACKNOWLEDGED
    env.manager.sync_fills()
    assert env.status(sell) == "ACKNOWLEDGED"
    assert env.scalar("select qty from positions") == 8
    monkeypatch.setattr(env.broker, "list_fills", original_fills)
    env.manager.sync_fills()
    assert env.status(sell) == "CANCELED"
    assert env.row(sell).filled_qty == 5
    assert env.scalar("select qty from positions") == 3
