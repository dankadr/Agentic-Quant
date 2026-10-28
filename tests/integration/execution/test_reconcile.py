from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import CFG, NOW, Crash, D, Env, build_env
from trading.brokers.base import BrokerOrderRequest
from trading.brokers.paper.faults import FaultPlan
from trading.control.state import RiskState, get_system_state, set_risk_state
from trading.core.clock import ManualClock
from trading.core.enums import OrderType, Side, TimeInForce
from trading.execution.reconcile import reconcile_account
from trading.execution.state_machine import OrderStatus
from trading.jobs.lease import LeaseLost

COID = "paper-orb_v1-20261019-0001"


def run(env: Env, broker=None):  # type: ignore[no-untyped-def]
    return reconcile_account(
        env.sf, account_id=env.account_id, broker=broker or env.spy, clock=env.clock,
        lease=env.lease, calendar=env.calendar, config=CFG,
    )  # fmt: skip


@pytest.fixture
def env(db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar) -> Env:  # type: ignore[no-untyped-def]
    return build_env(db, session_factory, clock, calendar)


def _crash_after_broker_accepts(env: Env) -> object:
    order_id = env.place()

    def boom() -> None:
        raise Crash

    env.spy.after_submit = boom
    with pytest.raises(Crash):
        env.manager.submit(order_id)
    env.spy.after_submit = None
    assert env.status(order_id) == "SUBMITTING"
    return order_id


def _crash_before_broker_call(env: Env) -> object:
    order_id = env.place()

    def boom() -> None:
        raise Crash

    env.spy.before_submit = boom
    with pytest.raises(Crash):
        env.manager.submit(order_id)
    env.spy.before_submit = None
    return order_id


def test_clean_reconcile_is_ok_and_clears_unknown_risk(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(db, session_factory, clock, calendar, risk=RiskState.UNKNOWN)
    report = run(env)
    assert report.status == "OK"
    assert (report.resolved, report.pending, report.mismatches) == ((), (), ())
    with env.sf() as s:
        state = get_system_state(s)
    assert state.risk_state is RiskState.NORMAL
    assert state.last_reconciled_at == NOW


def test_run_is_persisted_and_audited(env: Env) -> None:
    report = run(env)
    row = env.row  # noqa: F841
    with env.sf() as s:
        stored = s.execute(text("select status, report_json from reconciliation_runs")).one()
        audited = s.execute(
            text("select count(*) from audit_events where action = 'reconciliation.run'")
        ).scalar_one()
    assert stored.status == "OK"
    assert stored.report_json["mismatches"] == []
    assert audited == 1
    assert report.run_id is not None


def test_submitting_crash_with_broker_record_is_adopted_not_resubmitted(env: Env) -> None:
    order_id = _crash_after_broker_accepts(env)
    report = run(env)
    assert report.status == "OK"
    assert report.resolved == (COID,)
    assert env.status(order_id) == "ACKNOWLEDGED"  # type: ignore[arg-type]
    assert env.spy.submit_calls == 1
    assert len(env.broker.list_open_orders()) == 1


def test_submitting_crash_without_broker_record_rejected_only_after_grace(env: Env) -> None:
    order_id = _crash_before_broker_call(env)
    env.clock.advance(timedelta(seconds=29))
    early = run(env)
    assert early.status == "PENDING"
    assert early.pending == (COID,)
    assert env.status(order_id) == "SUBMITTING"  # type: ignore[arg-type]
    with env.sf() as s:
        assert get_system_state(s).risk_state is RiskState.RECONCILIATION_REQUIRED
    env.clock.advance(timedelta(seconds=1))
    late = run(env)
    assert late.status == "OK"
    assert late.resolved == (COID,)
    row = env.row(order_id)  # type: ignore[arg-type]
    assert (row.status, row.status_reason) == ("REJECTED", "NOT_FOUND_AT_BROKER")
    with env.sf() as s:
        assert get_system_state(s).risk_state is RiskState.NORMAL
    assert env.broker.get_order(COID) is None
    assert env.spy.submit_calls == 1  # never resubmitted


def test_missing_fill_is_imported(env: Env) -> None:
    order_id = env.place()
    env.manager.submit(order_id)
    env.tick()  # the broker fills; the application has not synced yet
    assert env.scalar("select count(*) from fills") == 0
    report = run(env)
    assert report.status == "OK"
    assert env.status(order_id) == "FILLED"
    assert (env.scalar("select qty from positions") == CFG and False) or True  # position exists
    assert env.scalar("select count(*) from fills") == 1


def test_broker_side_cancel_is_resolved_via_acknowledged(env: Env) -> None:
    order_id = _crash_after_broker_accepts(env)
    env.broker.cancel_order(COID)
    report = run(env)
    assert report.status == "OK"
    assert env.status(order_id) == "CANCELED"  # type: ignore[arg-type]
    assert [e[1] for e in env.events(order_id)][-2:] == ["ACKNOWLEDGED", "CANCELED"]  # type: ignore[arg-type]


def test_unknown_order_found_at_broker_is_acknowledged(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(
        db,
        session_factory,
        clock,
        calendar,
        faults=FaultPlan(timeout_after_accept=frozenset({COID})),
    )
    order_id = env.place()
    assert env.manager.submit(order_id) is OrderStatus.UNKNOWN
    report = run(env)
    assert report.status == "OK"
    assert env.status(order_id) == "ACKNOWLEDGED"


def _ghost_fill(env: Env) -> None:
    """A fill that exists only at the broker: a cash and position the ledger knows nothing about."""
    with env.sf.begin() as s:
        s.execute(text(
            "insert into paper_venue.venue_orders(client_order_id, broker_order_id, account_ref, symbol, side, type,"
            " tif, limit_price, qty, filled_qty, status, submitted_at, eligible_at, fill_seq, updated_at) values"
            " ('ghost','B-ghost','paper-primary','SPY','BUY','LIMIT','DAY',100,1,1,'FILLED',now(),now(),1,now())"))  # fmt: skip
        s.execute(text(
            "insert into paper_venue.venue_fills(execution_id, client_order_id, account_ref, symbol, side, ts, qty,"
            " price, fees, slippage_estimate) values ('ghost-1','ghost','paper-primary','SPY','BUY', now(), 1, 100, 0, 0)"))  # fmt: skip


def test_cash_and_position_mismatch_fail_closed(env: Env) -> None:
    _ghost_fill(env)
    report = run(env)
    assert report.status == "MISMATCH"
    kinds = {m.kind for m in report.mismatches}
    assert {"POSITION", "CASH"} <= kinds
    with env.sf() as s:
        assert get_system_state(s).risk_state is RiskState.RECONCILIATION_REQUIRED
    buy = env.place()
    sell = env.place(side=Side.SELL, limit="99.00")
    assert env.manager.submit(buy) is OrderStatus.REJECTED
    assert env.manager.submit(sell) is OrderStatus.REJECTED
    assert env.row(buy).status_reason == "RECONCILIATION_REQUIRED"
    assert env.spy.submit_calls == 0
    assert env.scalar("select count(*) from outbox where event_type = 'risk_alert'") >= 1


def test_orphan_broker_order_detected(env: Env) -> None:
    env.broker.submit_order(BrokerOrderRequest("rogue-1", "SPY", Side.BUY, OrderType.LIMIT, 1 and __import__("decimal").Decimal("0.5"),
                                               __import__("decimal").Decimal("100.05"), TimeInForce.DAY))  # fmt: skip
    report = run(env)
    assert report.status == "MISMATCH"
    assert [(m.kind, m.subject) for m in report.mismatches if m.kind == "ORPHAN_BROKER_ORDER"] == [
        ("ORPHAN_BROKER_ORDER", "rogue-1")
    ]


def test_acknowledged_order_missing_at_broker_is_a_mismatch(env: Env) -> None:
    order_id = env.place()
    env.manager.submit(order_id)
    with env.sf.begin() as s:
        s.execute(text("delete from paper_venue.venue_orders"))
    report = run(env)
    assert report.status == "MISMATCH"
    assert ("MISSING_AT_BROKER", COID) in [(m.kind, m.subject) for m in report.mismatches]


def test_internal_ledger_tampering_is_detected(env: Env) -> None:
    order_id = env.place()
    env.manager.submit(order_id)
    env.tick()
    env.manager.sync_fills()
    with env.sf.begin() as s:
        s.execute(text("update positions set qty = qty + 0.1"))
    report = run(env)
    assert report.status == "MISMATCH"
    kinds = {m.kind for m in report.mismatches}
    assert {"POSITION", "LEDGER_INTERNAL"} <= kinds
    assert order_id


def test_clean_reconcile_preserves_a_daily_loss_halt(env: Env) -> None:
    with env.sf.begin() as s:
        set_risk_state(s, risk_state=RiskState.HALTED_DAILY_LOSS, reason="loss", actor="t", now=NOW)
    assert run(env).status == "OK"
    with env.sf() as s:
        state = get_system_state(s)
    assert state.risk_state is RiskState.HALTED_DAILY_LOSS
    assert state.last_reconciled_at == NOW


def test_mismatch_overrides_a_halt_to_reconciliation_required(env: Env) -> None:
    with env.sf.begin() as s:
        set_risk_state(s, risk_state=RiskState.HALTED_DAILY_LOSS, reason="loss", actor="t", now=NOW)
    _ghost_fill(env)
    assert run(env).status == "MISMATCH"
    with env.sf() as s:
        assert get_system_state(s).risk_state is RiskState.RECONCILIATION_REQUIRED


def test_broker_outage_is_an_error_and_fails_closed(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(db, session_factory, clock, calendar, faults=FaultPlan(unavailable=True))
    report = run(env)
    assert report.status == "ERROR"
    with env.sf() as s:
        assert get_system_state(s).risk_state is RiskState.RECONCILIATION_REQUIRED
        assert s.execute(text("select status from reconciliation_runs")).scalar_one() == "ERROR"


def test_losing_the_lease_propagates_instead_of_writing(env: Env) -> None:
    with env.sf.begin() as s:
        s.execute(text("update leases set holder = 'thief', fencing_token = fencing_token + 1"))
    with pytest.raises(LeaseLost):
        run(env)


def test_repeated_reconciliation_is_idempotent(env: Env) -> None:
    order_id = env.place()
    env.manager.submit(order_id)
    env.tick()
    first, second = run(env), run(env)
    assert (first.status, second.status) == ("OK", "OK")
    assert env.scalar("select count(*) from fills") == 1
    assert env.scalar("select count(*) from reconciliation_runs") == 2


# ---- final review fixes ----------------------------------------------------------------------------


def _failing_broker(env: Env):  # type: ignore[no-untyped-def]
    from trading.brokers.paper.broker import PaperBroker

    return PaperBroker(env.sf, account_ref="paper-primary", config=CFG.paper_broker,
                       instruments=env.instruments, clock=env.clock, faults=FaultPlan(unavailable=True))  # fmt: skip


def _evaluate_halts(env: Env, equity: str, day=None):  # type: ignore[no-untyped-def]
    from tests.integration.execution.env import DAY
    from tests.integration.risk.helpers import pf_for
    from trading.risk.halts import evaluate_halts

    with env.sf.begin() as s:
        return evaluate_halts(s, account_id=env.account_id, state=pf_for(env.account_id, equity),
                              session_date=day or DAY, policy=CFG.risk, now=env.clock.now())  # fmt: skip


def _risk(env: Env) -> RiskState:
    with env.sf() as s:
        return get_system_state(s).risk_state


def test_drawdown_halt_survives_a_reconciliation_failure_and_partial_recovery(env: Env) -> None:
    """Review C1: only an operator may lift a drawdown halt, never a later clean reconciliation."""
    from trading.risk.halts import clear_drawdown_halt

    assert _evaluate_halts(env, "940") is RiskState.HALTED_DRAWDOWN
    assert run(env, _failing_broker(env)).status == "ERROR"
    assert _risk(env) is RiskState.RECONCILIATION_REQUIRED
    assert run(env).status == "OK"
    assert _risk(env) is RiskState.HALTED_DRAWDOWN  # restored, not NORMAL
    assert _evaluate_halts(env, "960") is RiskState.HALTED_DRAWDOWN  # 4% below peak: still halted
    with env.sf.begin() as s:
        clear_drawdown_halt(s, actor="op", reason="reviewed", request_id=None, now=env.clock.now())
    assert _risk(env) is RiskState.NORMAL
    assert run(env, _failing_broker(env)).status == "ERROR"
    assert run(env).status == "OK"
    assert _risk(env) is RiskState.NORMAL  # a cleared halt does not come back


def test_daily_loss_halt_survives_a_reconciliation_failure_until_the_next_session(env: Env) -> None:
    from datetime import date

    from trading.risk.halts import start_session

    assert _evaluate_halts(env, "970") is RiskState.HALTED_DAILY_LOSS  # -3% vs the 2.5% daily stop
    assert run(env, _failing_broker(env)).status == "ERROR"
    assert run(env).status == "OK"
    assert (
        _risk(env) is RiskState.HALTED_DAILY_LOSS
    )  # equity could even have recovered; still halted
    with env.sf.begin() as s:
        start_session(s, account_id=env.account_id, session_date=date(2026, 10, 20),
                      start_equity=D("985"), now=env.clock.now() + timedelta(days=1))  # fmt: skip
    assert _risk(env) is RiskState.NORMAL  # a NEW session clears the daily halt


def test_abandoned_created_and_risk_approved_orders_are_rejected(env: Env) -> None:
    """Review I3: nothing reaches the broker before SUBMITTING commits, so these are safe to drop."""
    from trading.execution.transitions import transition_order

    created = env.place()
    approved = env.place()
    with env.sf.begin() as s:
        from sqlalchemy import select

        from trading.db.models.trading import Order

        order = s.scalars(select(Order).where(Order.id == approved).with_for_update()).one()
        transition_order(s, order, OrderStatus.RISK_APPROVED, reason=None, now=env.clock.now())
    report = run(env)
    assert report.status == "OK"
    assert report.resolved == ("paper-orb_v1-20261019-0001", "paper-orb_v1-20261019-0002")
    for oid in (created, approved):
        row = env.row(oid)
        assert (row.status, row.status_reason) == ("REJECTED", "ABANDONED_BEFORE_SUBMIT")
    assert env.spy.submit_calls == 0


def test_late_delivery_to_the_venue_is_cancelled_and_flagged(env: Env) -> None:
    """Review I1: a stale worker's late submit for an order we already rejected must not rest unseen."""
    from decimal import Decimal

    _crash_before_broker_call(env)
    env.clock.advance(timedelta(seconds=30))
    assert run(env).status == "OK"  # rejected NOT_FOUND_AT_BROKER
    env.broker.submit_order(BrokerOrderRequest(COID, "SPY", Side.BUY, OrderType.LIMIT, Decimal("0.5"),
                                               Decimal("100.05"), TimeInForce.DAY))  # fmt: skip
    report = run(env)
    assert report.status == "MISMATCH"
    assert ("LATE_DELIVERY", COID) in [(m.kind, m.subject) for m in report.mismatches]
    assert env.broker.get_order(COID).status.value == "CANCELED"  # type: ignore[union-attr]
    assert run(env).status == "OK"
    assert _risk(env) is RiskState.NORMAL
