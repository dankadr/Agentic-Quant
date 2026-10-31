from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill, new_order, open_account
from trading.core.config import load_config
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.data.instruments import upsert_universe
from trading.data.snapshot import build_snapshot
from trading.db.models.market import MarketBar, MarketQuote
from trading.portfolio.ledger import apply_fill
from trading.portfolio.valuation import value_portfolio
from trading.quant.registry import seed_strategies
from trading.quant.source import StrategyIntentSource

CFG = load_config(Path("configs"))
D = Decimal
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=16, seconds=5)  # the 13:45 breakout bar has just closed


def t(minute: int) -> datetime:
    return OPEN + timedelta(minutes=minute)


class World:
    def __init__(self, db: Session, sf: sessionmaker[Session], calendar: MarketCalendar) -> None:
        self.db = db
        self.calendar = calendar
        self.instruments = upsert_universe(db, CFG.universe)
        seed_strategies(db, CFG, now=T0)
        db.execute(
            text("update system_state set trading_state = 'PAPER_RUNNING', risk_state = 'NORMAL'")
        )
        self.account_id = open_account(db)
        self.source = StrategyIntentSource(
            sf, account_id=self.account_id, calendar=calendar, config=CFG,
            instruments=self.instruments, provider_name="replay",
        )  # fmt: skip

    def bar(
        self,
        symbol: str,
        minute: int,
        o: str,
        h: str,
        lo: str,
        c: str,
        v: str = "1000",
        *,
        late: timedelta = timedelta(0),
    ) -> None:
        self.db.add(MarketBar(instrument_id=self.instruments[symbol].id, interval="1m", ts=t(minute), provider="replay",
                              open=D(o), high=D(h), low=D(lo), close=D(c), volume=D(v), ingested_at=t(minute + 1) + late,
                              session_date=t(0).date(), source_seq=None, quality_flags=[]))  # fmt: skip
        self.db.flush()

    def quote(self, symbol: str, at: datetime, bid: str, ask: str) -> None:
        self.db.add(MarketQuote(instrument_id=self.instruments[symbol].id, ts=at, provider="replay", bid=D(bid), ask=D(ask),
                                bid_size=None, ask_size=None, ingested_at=at, quality_flags=[]))  # fmt: skip
        self.db.flush()

    def orb_day(
        self, symbol: str = "SPY", *, breakout_volume: str = "1500", late: timedelta = timedelta(0)
    ) -> None:
        for m in range(15):
            self.bar(symbol, m, "100.00", "100.10", "99.90", "100.00", "1000")
        self.bar(symbol, 15, "100.05", "100.35", "100.05", "100.30", breakout_volume, late=late)
        self.quote(symbol, t(16) - timedelta(seconds=1), "100.29", "100.31")

    def propose(self, now: datetime = NOW):  # type: ignore[no-untyped-def]
        self.db.commit()  # the source runs in its own transactions
        snapshot = build_snapshot(
            self.db,
            symbols=[u.symbol for u in CFG.universe],
            as_of=now,
            provider="replay",
            max_bar_age_seconds=90,
        )
        portfolio = value_portfolio(self.db, self.account_id, marks={s: v.quote.mid for s, v in snapshot.symbols.items() if v.quote},
                                    ask_by_symbol={}, as_of=now, session_date=now.date(), cfg=CFG.paper_broker)  # fmt: skip
        self.db.commit()  # the snapshot row must be visible to the source's own transaction
        return self.source.proposals(snapshot=snapshot, portfolio=portfolio, now=now)

    def count(self, sql: str):  # type: ignore[no-untyped-def]
        return self.db.execute(text(sql)).scalar_one()


@pytest.fixture
def world(db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar) -> World:
    return World(db, session_factory, calendar)


def test_a_breakout_becomes_a_sized_buy_proposal_and_is_recorded(world: World) -> None:
    world.orb_day()
    (req,) = world.propose()
    assert (req.symbol, req.side, req.strategy_id) == ("SPY", Side.BUY, "orb_v1")
    assert req.limit_price == D("100.42")  # ask 100.31 + 10 bp, ceiled to the cent
    assert req.quantity == D("1.1949")  # floor(120 / 100.42, 0.0001)
    row = world.db.execute(
        text("select strategy_id, side, status, reason_codes, target_notional from signals")
    ).one()
    assert (row.strategy_id, row.side, row.status) == ("orb_v1", "BUY", "ALLOCATED")
    assert row.reason_codes == ["ORB_BREAK", "VOLUME_CONFIRM"]
    decision = world.db.execute(
        text("select targets_json, rationale_json from allocation_decisions")
    ).one()
    assert decision.targets_json["SPY"]["side"] == "BUY"
    assert decision.rationale_json[0]["outcome"] == "ALLOCATED"


def test_nothing_is_proposed_outside_the_session(world: World) -> None:
    world.orb_day()
    assert world.propose(OPEN - timedelta(hours=1)) == ()
    assert world.count("select count(*) from signals") == 0


def test_bars_ingested_after_the_decision_time_are_invisible(world: World) -> None:
    world.orb_day(late=timedelta(minutes=5))  # the breakout bar only arrives 5 minutes late
    assert world.propose() == ()
    assert world.count("select count(*) from signals") == 0


def test_no_signal_without_volume_confirmation(world: World) -> None:
    world.orb_day(breakout_volume="1000")
    assert world.propose() == ()


def test_one_entry_per_symbol_per_session_even_before_the_first_fill(world: World) -> None:
    world.orb_day()
    new_order(
        world.db,
        world.account_id,
        "o1",
        qty="0.5",
        limit="101",
        status="REJECTED",
        strategy_ids=["orb_v1"],
    )
    assert len(world.propose()) == 1  # a REJECTED entry never happened: try again
    new_order(
        world.db,
        world.account_id,
        "o2",
        qty="0.5",
        limit="101",
        status="ACKNOWLEDGED",
        strategy_ids=["orb_v1"],
    )
    assert world.propose() == ()  # an entry is open: no second one


def test_an_exit_already_in_flight_is_dropped_and_explained(world: World) -> None:
    world.orb_day()
    buy = new_order(
        world.db, world.account_id, "b1", qty="0.8", limit="101", strategy_ids=["orb_v1"]
    )
    apply_fill(world.db, account_id=world.account_id, fill=broker_fill(buy, "e1", qty="0.8", price="100.30"),
               settles_on=NEXT_DAY, now=t(16))  # fmt: skip
    new_order(
        world.db,
        world.account_id,
        "s1",
        side="SELL",
        qty="0.8",
        limit="99.00",
        status="ACKNOWLEDGED",
        strategy_ids=["orb_v1"],
    )
    for m in range(16, 20):
        world.bar("SPY", m, "100.00", "100.00", "99.70", "99.80", "800")
    world.quote("SPY", t(20) - timedelta(seconds=1), "99.79", "99.81")
    assert (
        world.propose(t(20) + timedelta(seconds=5)) == ()
    )  # the stop fired, but a SELL is already open
    assert "ORDER_IN_FLIGHT" in world.count("select rationale_json::text from allocation_decisions")
    assert world.count("select status from signals") == "IGNORED"


def test_holdings_come_from_recorded_attribution_and_drive_exits(world: World) -> None:
    world.orb_day()
    buy = new_order(
        world.db, world.account_id, "b1", qty="0.8", limit="101", strategy_ids=["orb_v1"]
    )
    apply_fill(world.db, account_id=world.account_id, fill=broker_fill(buy, "e1", qty="0.8", price="100.30"),
               settles_on=NEXT_DAY, now=t(16))  # fmt: skip
    # price falls through the opening-range low (99.90)
    for m in range(16, 20):
        world.bar("SPY", m, "100.00", "100.00", "99.70", "99.80", "800")
    world.quote("SPY", t(20) - timedelta(seconds=1), "99.79", "99.81")
    (req,) = world.propose(t(20) + timedelta(seconds=5))
    assert (req.side, req.quantity) == (Side.SELL, D("0.8"))
    assert req.attribution == {"orb_v1": D("-0.8")}
    assert world.db.execute(
        text("select reason_codes from signals where side = 'SELL'")
    ).scalar_one() == ["STOP"]


def test_everything_is_flattened_before_the_close_and_attributed_to_its_holders(
    world: World,
) -> None:
    world.orb_day()
    buy = new_order(
        world.db, world.account_id, "b1", qty="0.8", limit="101", strategy_ids=["orb_v1"]
    )
    apply_fill(world.db, account_id=world.account_id, fill=broker_fill(buy, "e1", qty="0.8", price="100.30"),
               settles_on=NEXT_DAY, now=t(16))  # fmt: skip
    info = world.calendar.session_on(OPEN.date())
    assert info is not None
    late = info.close_at - timedelta(minutes=8)
    world.bar("SPY", 380, "100.40", "100.50", "100.30", "100.45", "800")
    world.quote("SPY", late - timedelta(seconds=1), "100.44", "100.46")
    (req,) = world.propose(late)
    assert (req.side, req.symbol, req.quantity) == (Side.SELL, "SPY", D("0.8"))
    assert req.attribution == {"orb_v1": D("-0.8")}
    assert (
        world.db.execute(
            text("select strategy_id from signals where reason_codes = ARRAY['EOD_FLATTEN']")
        ).scalar_one()
        == "eod_flatten"
    )


def test_disabled_and_shadow_strategies_do_not_run(world: World) -> None:
    world.orb_day()
    world.db.execute(text("update strategies set enabled = false where id = 'orb_v1'"))
    assert world.propose() == ()


def test_parameters_come_from_the_strategies_table(world: World) -> None:
    world.orb_day()
    world.db.execute(
        text(
            "update strategies set params_json = jsonb_set(params_json, '{trade_notional}', '\"40\"') where id = 'orb_v1'"
        )
    )
    (req,) = world.propose()
    assert req.quantity == D("0.3983")  # floor(40 / 100.42)


def test_same_inputs_give_the_same_proposals(world: World) -> None:
    world.orb_day()
    first = world.propose()
    second = world.propose()
    assert [(r.symbol, r.side, r.quantity, r.limit_price) for r in first] == [
        (r.symbol, r.side, r.quantity, r.limit_price) for r in second
    ]


def test_entries_are_not_proposed_while_halted(world: World) -> None:
    """Review C2: strategies are unaware of halts; the allocator must not emit doomed entries."""
    world.orb_day()
    world.db.execute(text("update system_state set risk_state = 'HALTED_DAILY_LOSS'"))
    assert world.propose() == ()
    assert "ENTRIES_BLOCKED" in world.count("select rationale_json::text from allocation_decisions")


class _Boom:
    def __init__(self, *, strategy_id: str, version: str, params: object) -> None:
        self.strategy_id, self.version = strategy_id, version

    def evaluate(self, data: object) -> list[object]:
        raise RuntimeError("strategy blew up")


def test_one_failing_strategy_does_not_stop_the_others_or_the_flatten(world: World) -> None:
    """Review I6: a bad strategy must never abort the tick, least of all the end-of-day flatten."""
    from trading.quant.source import DEFAULT_FACTORIES

    world.source = StrategyIntentSource(
        world.source._sf,
        account_id=world.account_id,
        calendar=world.calendar,
        config=CFG,
        instruments=world.instruments,
        provider_name="replay",
        factories={**DEFAULT_FACTORIES, "orb_v1": _Boom},  # type: ignore[dict-item]
    )
    world.orb_day()
    buy = new_order(
        world.db, world.account_id, "b1", qty="0.8", limit="101", strategy_ids=["orb_v1"]
    )
    apply_fill(world.db, account_id=world.account_id, fill=broker_fill(buy, "e1", qty="0.8", price="100.30"),
               settles_on=NEXT_DAY, now=t(16))  # fmt: skip
    info = world.calendar.session_on(OPEN.date())
    assert info is not None
    late = info.close_at - timedelta(minutes=8)
    world.bar("SPY", 380, "100.40", "100.50", "100.30", "100.45", "800")
    world.quote("SPY", late - timedelta(seconds=1), "100.44", "100.46")
    (req,) = world.propose(late)
    assert (req.side, req.symbol, req.quantity) == (Side.SELL, "SPY", D("0.8"))


def _macro_world(world: World, *, required: bool = True, min_multiplier: float = 0.0) -> None:
    from trading.core.config import AgentsConfig

    agents = AgentsConfig.model_validate(
        {"macro": {"enabled": True, "required": required, "min_multiplier": min_multiplier}}
    )
    cfg = CFG.model_copy(update={"agents": agents})
    world.source = StrategyIntentSource(
        world.source._sf,
        account_id=world.account_id,
        calendar=world.calendar,
        config=cfg,
        instruments=world.instruments,
        provider_name="replay",
    )


def _macro_run(world: World, multiplier: str, *, valid_minutes: int = 60) -> None:
    import json

    result = {
        "regime": "RANGING",
        "risk_multiplier": multiplier,
        "valid_until": (NOW + timedelta(minutes=valid_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    world.db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at, completed_at,"
            " available_at, result_json, status) values (gen_random_uuid(), 'macro', 'm', 'macro-v1',"
            " :t, :t, :t, CAST(:r AS jsonb), 'COMPLETED')"
        ),
        {"t": NOW - timedelta(minutes=1), "r": json.dumps(result)},
    )
    world.db.commit()


def test_required_macro_view_missing_blocks_new_entries(world: World) -> None:
    """Spec: stale or missing macro view => no new entries for dependent strategies."""
    _macro_world(world)
    world.orb_day()
    assert world.propose() == ()
    assert "MACRO_BLOCK" in world.count("select rationale_json::text from allocation_decisions")


def test_a_fresh_macro_view_scales_entries_down(world: World) -> None:
    world.orb_day()
    (full,) = world.propose()
    world.db.execute(text("delete from allocation_decisions"))
    world.db.execute(text("delete from signals"))
    world.db.commit()
    _macro_world(world)
    _macro_run(world, "0.5")
    (half,) = world.propose()
    assert half.quantity < full.quantity
    assert half.quantity * D("100.31") <= D("60")


def _full_quantity(world: World) -> D:
    world.orb_day()
    (full,) = world.propose()
    world.db.execute(text("delete from allocation_decisions"))
    world.db.execute(text("delete from signals"))
    world.db.commit()
    return full.quantity


def test_the_macro_floor_limits_how_far_caution_shrinks_an_entry(world: World) -> None:
    full = _full_quantity(world)
    _macro_world(world, min_multiplier=0.7)
    _macro_run(world, "0.5")
    (floored,) = world.propose()
    assert full * D("0.69") < floored.quantity <= full * D("0.70")


def test_a_view_above_the_macro_floor_is_used_as_is(world: World) -> None:
    full = _full_quantity(world)
    _macro_world(world, min_multiplier=0.7)
    _macro_run(world, "0.9")
    (scaled,) = world.propose()
    assert full * D("0.89") < scaled.quantity <= full * D("0.90")


def test_the_macro_floor_never_overrides_a_block(world: World) -> None:
    world.orb_day()
    _macro_world(world, min_multiplier=0.7)
    _macro_run(world, "0")
    assert world.propose() == ()
    assert "MACRO_BLOCK" in world.count("select rationale_json::text from allocation_decisions")


def test_a_required_macro_view_missing_still_blocks_with_a_floor(world: World) -> None:
    world.orb_day()
    _macro_world(world, min_multiplier=0.7)
    assert world.propose() == ()


def test_an_expired_macro_view_blocks_when_required_and_is_ignored_when_not(world: World) -> None:
    world.orb_day()
    _macro_world(world, required=False)
    assert len(world.propose()) == 1  # not required: a missing view means multiplier 1
    world.db.execute(text("delete from allocation_decisions"))
    world.db.execute(text("delete from signals"))
    world.db.commit()
    _macro_world(world, required=True)
    _macro_run(world, "1.0", valid_minutes=-5)  # already expired
    assert world.propose() == ()
