"""Incidents, their deliveries and recovery, and the watchdog, against PostgreSQL and a fake Telegram."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.alerts.store import raise_incident, resolve_incident
from trading.alerts.telegram import MAX_ATTEMPTS, dispatch_due
from trading.alerts.watchdog import run_watchdog
from trading.core.clock import ManualClock
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
TOKEN = "123456:SECRET-BOT-TOKEN"


def _deliveries(db: Session) -> list[tuple[str, str]]:
    db.rollback()
    return [
        tuple(r)
        for r in db.execute(
            text("select event, state from incident_deliveries order by created_at, event")
        ).all()
    ]


def test_an_open_condition_alerts_once_and_recovers_once(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        assert raise_incident(s, key="k", severity="CRITICAL", title="down", detail="x", now=NOW)
    with session_factory.begin() as s:
        assert not raise_incident(
            s, key="k", severity="CRITICAL", title="down", detail="x", now=NOW
        )
    with session_factory.begin() as s:
        assert resolve_incident(s, key="k", now=NOW)
    with session_factory.begin() as s:
        assert not resolve_incident(s, key="k", now=NOW)
    assert _deliveries(db) == [("OPENED", "PENDING"), ("RECOVERED", "PENDING")]


def test_a_stuck_telegram_is_retried_then_marked_failed_and_never_blocks(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        raise_incident(s, key="k", severity="WARNING", title="t", detail="d", now=NOW)

    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(down))
    now = NOW
    for _ in range(MAX_ATTEMPTS + 2):
        dispatch_due(session_factory, client, bot_token=TOKEN, chat_id="1", now=now)
        now += timedelta(hours=1)  # past every backoff
    assert _deliveries(db) == [("OPENED", "FAILED")]
    stored = db.execute(text("select last_error, attempts from incident_deliveries")).one()
    assert stored[0] == "HTTPError"  # the type only, never the URL that carries the token
    assert stored[1] == MAX_ATTEMPTS


def test_a_delivery_sends_only_the_incident_text_and_marks_it_sent(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        raise_incident(
            s, key="k", severity="CRITICAL", title="Lease missing", detail="check w", now=NOW
        )
    sent: list[httpx.Request] = []

    def ok(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(ok))
    assert dispatch_due(session_factory, client, bot_token=TOKEN, chat_id="42", now=NOW) == 1
    body = sent[0].read().decode()
    assert "[CRITICAL] Lease missing" in body and '"chat_id":"42"' in body
    assert "report" not in body.lower() and "payload" not in body.lower()
    assert _deliveries(db) == [("OPENED", "SENT")]


def test_the_watchdog_raises_when_no_executor_holds_the_lease_and_clears_when_one_does(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    clock = ManualClock(NOW)
    run_watchdog(session_factory, clock=clock, calendar=None, market_data_state="ok")
    run_watchdog(session_factory, clock=clock, calendar=None, market_data_state="ok")
    db.rollback()
    open_count = db.execute(
        text("select count(*) from incidents where state = 'OPEN'")
    ).scalar_one()
    assert open_count == 1  # repeated detection is one incident, not one per minute

    with session_factory.begin() as s:
        acquire_lease(s, name=EXECUTOR_LEASE, holder="w1", now=NOW, ttl=timedelta(hours=1))
    run_watchdog(session_factory, clock=clock, calendar=None, market_data_state="ok")
    assert _deliveries(db) == [("OPENED", "PENDING"), ("RECOVERED", "PENDING")]
