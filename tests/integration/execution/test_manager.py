from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import CFG, DAY, NOW, D, Env, build_env
from trading.brokers.base import BrokerOrderStatus
from trading.brokers.paper.faults import FaultPlan
from trading.control.state import (
    RiskState,
    TradingState,
    get_system_state,
    set_risk_state,
    set_trading_state,
)
from trading.core.clock import ManualClock
from trading.core.enums import OrderType, Side, TimeInForce
from trading.execution.manager import DecisionAlreadyUsed, RiskNotApproved
from trading.execution.state_machine import OrderStatus
from trading.jobs.lease import LeaseLost


@pytest.fixture
def env(db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar) -> Env:  # type: ignore[no-untyped-def]
    return build_env(db, session_factory, clock, calendar)


# ---- create_order -------------------------------------------------------------------------------


def test_create_order_writes_intent_order_event_and_outbox(env: Env) -> None:
    order_id = env.place()
    row = env.row(order_id)
    assert (row.status, row.client_order_id, row.qty, row.filled_qty) == (
        "CREATED", "paper-orb_v1-20261019-0001", D("0.5"), D("0")
    )  # fmt: skip
    intent = env.scalar(
        "select count(*) from order_intents where is_entry and risk_decision_id is not null"
    )
    assert intent == 1
    assert env.events(order_id) == [(None, "CREATED")]
    assert env.scalar("select count(*) from outbox where event_type = 'order_created'") == 1


def test_create_order_requires_allowed_decision(env: Env) -> None:
    with pytest.raises(RiskNotApproved):
        env.place(allowed=False)
    assert env.scalar("select count(*) from order_intents") == 0
    assert env.scalar("select count(*) from orders") == 0


def test_in_memory_approval_is_not_trusted_without_a_matching_persisted_decision(env: Env) -> None:
    from dataclasses import replace

    from tests.factories import make_risk_decision, make_snapshot
    from trading.core.enums import OrderType, TimeInForce
    from trading.db.models.market import Instrument
    from trading.db.models.portfolio import Account
    from trading.execution.manager import ExecutionManager  # noqa: F401
    from trading.risk.models import ProposedOrder, RiskEvaluation
    from trading.risk.service import RecordedDecision

    info = env.instruments["SPY"]
    with env.sf.begin() as s:
        account = s.get(Account, env.account_id)
        instrument = s.get(Instrument, info.id)
        snapshot = make_snapshot(s, now=NOW, data_hash="forged")
        denied = make_risk_decision(
            s, account, instrument, snapshot, allowed=False, dedupe_key="forged", now=NOW
        )  # type: ignore[arg-type]
        forged = RecordedDecision(
            denied.id, RiskEvaluation(True, (), "v1", True, None)
        )  # claims allowed
        proposal = ProposedOrder(env.account_id, info, Side.BUY, D("0.5"), OrderType.LIMIT, D("100.05"),
                                 TimeInForce.DAY, "orb_v1", None, None, "forged")  # fmt: skip
        with pytest.raises(RiskNotApproved):
            env.manager.create_order(
                s,
                proposal=proposal,
                decision=forged,
                market_snapshot_id=snapshot.id,
                session_date=DAY,
            )
        assert replace  # keep import used
    assert env.scalar("select count(*) from order_intents") == 0


def test_decision_must_match_the_proposal(env: Env) -> None:
    from tests.factories import make_risk_decision, make_snapshot
    from trading.core.enums import OrderType, TimeInForce
    from trading.db.models.market import Instrument
    from trading.db.models.portfolio import Account
    from trading.risk.models import ProposedOrder, RiskEvaluation
    from trading.risk.service import RecordedDecision

    spy, qqq = env.instruments["SPY"], env.instruments["QQQ"]
    with env.sf.begin() as s:
        account = s.get(Account, env.account_id)
        snapshot = make_snapshot(s, now=NOW, data_hash="m")
        row = make_risk_decision(
            s, account, s.get(Instrument, spy.id), snapshot, dedupe_key="dk", now=NOW
        )  # type: ignore[arg-type]
        decision = RecordedDecision(row.id, RiskEvaluation(True, (), "v1", True, None))
        wrong_symbol = ProposedOrder(env.account_id, qqq, Side.BUY, D("0.5"), OrderType.LIMIT, D("100.05"),
                                     TimeInForce.DAY, "orb_v1", None, None, "dk")  # fmt: skip
        with pytest.raises(RiskNotApproved, match="instrument"):
            env.manager.create_order(
                s,
                proposal=wrong_symbol,
                decision=decision,
                market_snapshot_id=snapshot.id,
                session_date=DAY,
            )
        other_snapshot = make_snapshot(s, now=NOW, data_hash="other")
        right_symbol = ProposedOrder(env.account_id, spy, Side.BUY, D("0.5"), OrderType.LIMIT, D("100.05"),
                                     TimeInForce.DAY, "orb_v1", None, None, "dk")  # fmt: skip
        with pytest.raises(RiskNotApproved, match="snapshot"):
            env.manager.create_order(
                s,
                proposal=right_symbol,
                decision=decision,
                market_snapshot_id=other_snapshot.id,
                session_date=DAY,
            )


def _terms(info) -> dict:  # type: ignore[no-untyped-def,type-arg]
    import json
    from uuid import uuid4

    from trading.core.hashing import canonical_json
    from trading.risk.models import ProposedOrder
    from trading.risk.service import proposal_terms

    return json.loads(canonical_json(proposal_terms(ProposedOrder(
        uuid4(), info, Side.BUY, D("0.5"), OrderType.LIMIT, D("100.05"), TimeInForce.DAY, "orb_v1", None, None, "dk",
    ))))  # fmt: skip


def test_decision_cannot_be_reused(env: Env) -> None:
    from tests.factories import make_risk_decision, make_snapshot
    from trading.core.enums import OrderType, TimeInForce
    from trading.db.models.market import Instrument
    from trading.db.models.portfolio import Account
    from trading.risk.models import ProposedOrder, RiskEvaluation
    from trading.risk.service import RecordedDecision

    info = env.instruments["SPY"]
    with env.sf.begin() as s:
        snapshot = make_snapshot(s, now=NOW, data_hash="m")
        row = make_risk_decision(
            s,
            s.get(Account, env.account_id),
            s.get(Instrument, info.id),
            snapshot,
            dedupe_key="dk",
            now=NOW,
            proposal_json=_terms(info),
        )  # type: ignore[arg-type]
        decision = RecordedDecision(row.id, RiskEvaluation(True, (), "v1", True, None))
        proposal = ProposedOrder(env.account_id, info, Side.BUY, D("0.5"), OrderType.LIMIT, D("100.05"),
                                 TimeInForce.DAY, "orb_v1", None, None, "dk")  # fmt: skip
        env.manager.create_order(
            s,
            proposal=proposal,
            decision=decision,
            market_snapshot_id=snapshot.id,
            session_date=DAY,
        )
        with pytest.raises(DecisionAlreadyUsed):
            env.manager.create_order(
                s,
                proposal=proposal,
                decision=decision,
                market_snapshot_id=snapshot.id,
                session_date=DAY,
            )
    assert env.scalar("select count(*) from orders") == 1


def test_client_order_ids_sequential_per_strategy_and_day(env: Env) -> None:
    a, b = env.place(), env.place()
    other = env.place(strategy="orbXv1")  # '_' must not behave like a LIKE wildcard
    c = env.place()
    ids = [env.row(i, "client_order_id").client_order_id for i in (a, b, other, c)]
    assert ids == [
        "paper-orb_v1-20261019-0001", "paper-orb_v1-20261019-0002",
        "paper-orbXv1-20261019-0001", "paper-orb_v1-20261019-0003",
    ]  # fmt: skip


def test_entry_count_incremented_for_entries_only(env: Env) -> None:
    env.place()
    env.place(side=Side.SELL, limit="99.00", entry=False)
    assert env.scalar("select new_entries from trading_sessions") == 1


def test_create_order_with_stale_lease_raises(env: Env) -> None:
    _steal_lease(env)
    with pytest.raises(LeaseLost):
        env.place()
    assert env.scalar("select count(*) from orders") == 0


def _steal_lease(env: Env) -> None:
    with env.sf.begin() as s:
        s.execute(text("update leases set holder = 'thief', fencing_token = fencing_token + 1"))


# ---- submit -------------------------------------------------------------------------------------


def test_submit_happy_path_acknowledged(env: Env) -> None:
    order_id = env.place()
    assert env.manager.submit(order_id) is OrderStatus.ACKNOWLEDGED
    row = env.row(order_id)
    assert (row.status, row.broker_order_id) == ("ACKNOWLEDGED", "B-paper-orb_v1-20261019-0001")
    assert [e[1] for e in env.events(order_id)] == [
        "CREATED",
        "RISK_APPROVED",
        "SUBMITTING",
        "ACKNOWLEDGED",
    ]
    assert env.broker.get_order("paper-orb_v1-20261019-0001") is not None


def test_double_submit_makes_a_single_venue_order(env: Env) -> None:
    order_id = env.place()
    env.manager.submit(order_id)
    assert env.manager.submit(order_id) is OrderStatus.ACKNOWLEDGED
    assert env.spy.submit_calls == 1
    assert len(env.broker.list_open_orders()) == 1


def test_broker_reject_marks_rejected_without_touching_positions(env: Env) -> None:
    order_id = env.place(qty="20", limit="100.00")  # more than the venue's cash
    assert env.manager.submit(order_id) is OrderStatus.REJECTED
    row = env.row(order_id)
    assert (row.status, row.status_reason) == ("REJECTED", "INSUFFICIENT_FUNDS")
    assert env.scalar("select count(*) from positions") == 0
    assert env.scalar("select count(*) from fills") == 0
    assert env.scalar("select count(*) from cash_ledger") == 1  # initial deposit only


@pytest.mark.parametrize(
    "faults",
    [FaultPlan(timeout_after_accept=frozenset({"paper-orb_v1-20261019-0001"})),
     FaultPlan(timeout_before_accept=frozenset({"paper-orb_v1-20261019-0001"})),
     FaultPlan(unavailable=True)],
)  # fmt: skip
def test_transport_failure_marks_unknown(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar,
    faults: FaultPlan,
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(db, session_factory, clock, calendar, faults=faults)
    order_id = env.place()
    assert env.manager.submit(order_id) is OrderStatus.UNKNOWN
    row = env.row(order_id)
    assert (row.status, row.status_reason) == (
        "UNKNOWN",
        "BROKER_TIMEOUT" if not faults.unavailable else "BROKER_UNAVAILABLE",
    )
    assert env.manager.submit(order_id) is OrderStatus.UNKNOWN  # never blindly resubmitted
    assert env.spy.submit_calls == 1


def test_expired_intent_rejected_before_broker(env: Env) -> None:
    order_id = env.place(expires=NOW + timedelta(seconds=30))
    env.clock.advance(timedelta(seconds=30))
    assert env.manager.submit(order_id) is OrderStatus.REJECTED
    assert env.row(order_id).status_reason == "SIGNAL_EXPIRED"
    assert env.spy.submit_calls == 0


def test_paused_rejects_entry_before_broker(env: Env) -> None:
    order_id = env.place()
    with env.sf.begin() as s:
        set_trading_state(s, target=TradingState.PAUSED, expected_version=get_system_state(s).version,
                          actor="op", reason="stop", request_id=None, now=NOW)  # fmt: skip
    assert env.manager.submit(order_id) is OrderStatus.REJECTED
    assert env.row(order_id).status_reason == "TRADING_PAUSED"
    assert env.spy.submit_calls == 0


@pytest.mark.parametrize(
    ("risk", "reason"),
    [(RiskState.UNKNOWN, "RISK_STATE_UNKNOWN"), (RiskState.RECONCILIATION_REQUIRED, "RECONCILIATION_REQUIRED"),
     (RiskState.HALTED_DAILY_LOSS, "DAILY_LOSS_LIMIT"), (RiskState.HALTED_DRAWDOWN, "DRAWDOWN_LIMIT")],
)  # fmt: skip
def test_risk_state_blocks_entries_at_submit_time(env: Env, risk: RiskState, reason: str) -> None:
    order_id = env.place()
    with env.sf.begin() as s:
        set_risk_state(s, risk_state=risk, reason="changed after approval", actor="t", now=NOW)
    assert env.manager.submit(order_id) is OrderStatus.REJECTED
    assert env.row(order_id).status_reason == reason
    assert env.spy.submit_calls == 0


def _hold_position(env: Env) -> None:
    buy = env.place(qty="0.5", limit="100.05")
    env.manager.submit(buy)
    env.tick()
    assert env.manager.sync_fills() == 1


@pytest.mark.parametrize(
    "risk", [RiskState.NORMAL, RiskState.HALTED_DAILY_LOSS, RiskState.HALTED_DRAWDOWN]
)
def test_exits_allowed_during_halts(env: Env, risk: RiskState) -> None:
    _hold_position(env)
    with env.sf.begin() as s:
        set_risk_state(s, risk_state=risk, reason="halt", actor="t", now=env.clock.now())
    sell = env.place(side=Side.SELL, qty="0.5", limit="50.00")
    assert env.manager.submit(sell) is OrderStatus.ACKNOWLEDGED


@pytest.mark.parametrize("risk", [RiskState.UNKNOWN, RiskState.RECONCILIATION_REQUIRED])
def test_exits_blocked_when_state_is_untrusted(env: Env, risk: RiskState) -> None:
    _hold_position(env)
    with env.sf.begin() as s:
        set_risk_state(s, risk_state=risk, reason="ambiguous", actor="t", now=env.clock.now())
    sell = env.place(side=Side.SELL, qty="0.5", limit="50.00")
    assert env.manager.submit(sell) is OrderStatus.REJECTED


def test_submit_with_stale_lease_never_reaches_broker(env: Env) -> None:
    order_id = env.place()
    _steal_lease(env)
    with pytest.raises(LeaseLost):
        env.manager.submit(order_id)
    assert env.spy.submit_calls == 0
    assert env.status(order_id) == "CREATED"
    assert env.broker.get_order("paper-orb_v1-20261019-0001") is None


def test_lease_lost_during_the_broker_call_leaves_submitting_for_reconciliation(env: Env) -> None:
    order_id = env.place()
    env.spy.before_submit = lambda: _steal_lease(env)
    with pytest.raises(LeaseLost):
        env.manager.submit(order_id)
    assert env.status(order_id) == "SUBMITTING"  # the new leader's reconciliation resolves it
    assert env.broker.get_order("paper-orb_v1-20261019-0001") is not None


def test_submit_unknown_order_id_raises(env: Env) -> None:
    from uuid import uuid4

    with pytest.raises(LookupError):
        env.manager.submit(uuid4())


# ---- fills / cancel ----------------------------------------------------------------------------


def test_sync_fills_applies_once_and_updates_position(env: Env) -> None:
    order_id = env.place(qty="0.5", limit="100.05")
    env.manager.submit(order_id)
    env.tick()
    assert env.manager.sync_fills() == 1
    assert env.manager.sync_fills() == 0
    row = env.row(order_id)
    assert (row.status, row.filled_qty, row.avg_fill_price) == (
        "FILLED",
        D("0.5"),
        D("100.02000000"),
    )
    assert env.scalar("select qty from positions") == D("0.5")


def test_sync_fills_handles_partials_across_ticks(env: Env) -> None:
    order_id = env.place(qty="8", limit="101.00")  # needs two ticks at 5% participation of 100
    env.manager.submit(order_id)
    env.tick(volume="100")
    assert env.manager.sync_fills() == 1
    assert env.status(order_id) == "PARTIALLY_FILLED"
    env.tick(volume="100")
    assert env.manager.sync_fills() == 1
    assert env.status(order_id) == "FILLED"
    assert env.scalar("select count(*) from fills") == 2


def test_sale_proceeds_settle_on_the_next_session(env: Env) -> None:
    _hold_position(env)
    sell = env.place(side=Side.SELL, qty="0.5", limit="50.00")
    env.manager.submit(sell)
    env.tick(bid="101.00", ask="101.02")
    env.manager.sync_fills()
    settles = env.scalar(
        "select settles_on from cash_ledger where amount > 0 and source_type = 'TRADE'"
    )
    assert settles == DAY + timedelta(days=1)


def test_sync_fills_picks_up_broker_side_expiry(env: Env) -> None:
    order_id = env.place(limit="99.00")  # will not fill
    env.manager.submit(order_id)
    env.tick()
    env.broker.expire_day_orders(session_close=env.clock.now() + timedelta(hours=5))
    env.manager.sync_fills()
    assert env.status(order_id) == "EXPIRED"


def test_cancel_flows(env: Env) -> None:
    created = env.place()
    approved_never_submitted = env.place()
    open_order = env.place(limit="99.00")
    env.manager.submit(open_order)
    assert env.manager.cancel(created, reason="operator") is OrderStatus.REJECTED
    assert env.row(created).status_reason == "operator"
    assert env.manager.cancel(open_order, reason="operator") is OrderStatus.CANCELED
    assert env.broker.get_order("paper-orb_v1-20261019-0003").status is BrokerOrderStatus.CANCELED  # type: ignore[union-attr]
    assert env.manager.cancel(open_order, reason="again") is OrderStatus.CANCELED  # idempotent
    assert env.status(approved_never_submitted) == "CREATED"


def test_cancel_stale_entries_cancels_old_entries_only(env: Env) -> None:
    bought = env.place()
    env.manager.submit(bought)
    env.tick()  # fills, so there is a position to sell
    env.manager.sync_fills()
    exit_order = env.place(side=Side.SELL, limit="101.00")
    stale_entry = env.place(limit="99.00")
    env.manager.submit(exit_order)
    env.manager.submit(stale_entry)
    ttl = timedelta(seconds=120)
    env.clock.advance(timedelta(seconds=60))
    assert env.manager.cancel_stale_entries(ttl=ttl) == 0  # not old enough yet
    fresh_entry = env.place(limit="99.00")
    env.manager.submit(fresh_entry)
    env.clock.advance(timedelta(seconds=90))
    assert env.manager.cancel_stale_entries(ttl=ttl) == 1
    assert env.status(stale_entry) == "CANCELED"
    assert env.row(stale_entry).status_reason == "ENTRY_ORDER_TTL"
    assert env.status(fresh_entry) == "ACKNOWLEDGED"
    assert env.status(exit_order) == "ACKNOWLEDGED"  # exits are never cancelled by the TTL


def test_cancel_refuses_orders_in_limbo(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(db, session_factory, clock, calendar, faults=FaultPlan(unavailable=True))
    order_id = env.place()
    assert env.manager.submit(order_id) is OrderStatus.UNKNOWN
    assert env.manager.cancel(order_id, reason="x") is OrderStatus.UNKNOWN


def test_metrics_count_status_transitions(env: Env) -> None:
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("trading_orders_total", {"status": "ACKNOWLEDGED"}) or 0
    env.manager.submit(env.place())
    assert (
        REGISTRY.get_sample_value("trading_orders_total", {"status": "ACKNOWLEDGED"}) == before + 1
    )
    assert CFG.system.mode == "PAPER_ONLY"


# ---- hardening from the automated security review -----------------------------------------------


@pytest.mark.parametrize(
    "bad", ["orb-v1", "orb v1", "", "a/b", "orb_v1-20261019", "1orb", "x" * 65]
)
def test_strategy_id_must_be_a_safe_identifier(env: Env, bad: str) -> None:
    from trading.execution.manager import InvalidStrategyId

    with pytest.raises(InvalidStrategyId):
        env.place(strategy=bad)
    assert env.scalar("select count(*) from order_intents") == 0


def test_client_order_id_collision_is_not_misreported_as_decision_reuse(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    env.place()
    monkeypatch.setattr(
        "trading.execution.manager.make_client_order_id", lambda *a: "paper-orb_v1-20261019-0001"
    )
    with pytest.raises(IntegrityError):
        env.place()
    assert env.scalar("select count(*) from orders") == 1


# ---- final review: tie the order to the decision, and fence right before the broker call ----------


@pytest.mark.parametrize(
    "tampered",
    [{"quantity": "0.1"}, {"side": "SELL"}, {"limit_price": "90.00"}, {"strategy_id": "other_v1"}, {"symbol": "QQQ"},
     {"order_type": "MARKET"}],
)  # fmt: skip
def test_order_terms_must_match_the_recorded_decision(env: Env, tampered: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(RiskNotApproved, match="terms"):
        env.place(stored_terms=tampered)
    assert env.scalar("select count(*) from order_intents") == 0


def test_a_stale_decision_cannot_be_used(env: Env) -> None:
    with pytest.raises(RiskNotApproved, match="too old"):
        env.place(decision_age=timedelta(minutes=10))
    assert env.place(decision_age=timedelta(seconds=30))  # a fresh-enough decision is fine


def test_lease_lost_just_before_the_broker_call_never_reaches_the_broker(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = env.place()
    original = env.manager._prepare_submit

    def prepare_then_lose_the_lease(oid):  # type: ignore[no-untyped-def]
        result = original(oid)
        _steal_lease(env)  # e.g. the process was paused right after committing SUBMITTING
        return result

    monkeypatch.setattr(env.manager, "_prepare_submit", prepare_then_lose_the_lease)
    with pytest.raises(LeaseLost):
        env.manager.submit(order_id)
    assert env.spy.submit_calls == 0
    assert env.broker.get_order("paper-orb_v1-20261019-0001") is None
    assert env.status(order_id) == "SUBMITTING"  # the next leader's reconciliation resolves it


def test_netted_order_records_every_contributing_strategy(env: Env) -> None:
    oid = env.place(strategy_ids=("orb_v1", "vwap_v1"))
    assert env.scalar("select strategy_ids from order_intents") == ["orb_v1", "vwap_v1"]
    assert (
        env.row(oid).client_order_id == "paper-orb_v1-20261019-0001"
    )  # primary strategy names the order


def test_strategy_ids_are_part_of_the_checked_terms(env: Env) -> None:
    with pytest.raises(RiskNotApproved, match="terms"):
        env.place(strategy_ids=("orb_v1", "vwap_v1"), stored_terms={"strategy_ids": ["orb_v1"]})


def test_every_contributing_strategy_id_must_be_a_safe_identifier(env: Env) -> None:
    from trading.execution.manager import InvalidStrategyId

    with pytest.raises(InvalidStrategyId):
        env.place(strategy_ids=("orb_v1", "bad-id"))
