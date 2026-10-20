from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.core.hashing import content_hash
from trading.jobs.outbox import dispatch_pending, emit_event
from trading.monitoring.audit import list_audit, record_audit


def _audit(db: Session, clock: ManualClock, action: str = "a", **kw: Any):  # type: ignore[no-untyped-def]
    return record_audit(
        db,
        actor="op",
        action=action,
        target="t",
        request_id=kw.pop("request_id", None),
        before=kw.pop("before", None),
        after=kw.pop("after", None),
        result="ok",
        now=clock.now(),
        **kw,
    )


def test_audit_hashes_not_raw(db: Session, clock: ManualClock) -> None:
    eid = _audit(
        db, clock, before={"state": "PAUSED"}, after={"state": "PAPER_RUNNING"}, request_id="r1"
    )
    db.flush()
    row = list_audit(db, limit=10, before=None)[0]
    assert row.id == eid
    assert row.before_hash == content_hash({"state": "PAUSED"})
    assert row.after_hash == content_hash({"state": "PAPER_RUNNING"})
    assert row.request_id == "r1"
    assert "PAUSED" not in str(row.details_json)


def test_audit_none_before_after_store_null_hashes(db: Session, clock: ManualClock) -> None:
    _audit(db, clock)
    db.flush()
    row = list_audit(db, limit=1, before=None)[0]
    assert row.before_hash is None
    assert row.after_hash is None


def test_audit_details_are_redacted(db: Session, clock: ManualClock) -> None:
    _audit(db, clock, details={"api_key": "trk_abc", "note": "Bearer xyz", "n": 1})
    db.flush()
    details = list_audit(db, limit=1, before=None)[0].details_json
    assert details == {"api_key": "***", "note": "***", "n": 1}


def test_list_audit_keyset_pagination(db: Session, clock: ManualClock) -> None:
    ids = []
    for i in range(5):
        clock.advance(timedelta(seconds=1))
        ids.append(_audit(db, clock, action=f"a{i}"))
    db.flush()
    seen = []
    cursor = None
    for _ in range(3):
        page = list_audit(db, limit=2, before=cursor)
        seen += [r.id for r in page]
        if page:
            cursor = (page[-1].ts, page[-1].id)
    assert seen == list(reversed(ids))
    assert list_audit(db, limit=2, before=cursor) == []


def test_list_audit_ties_broken_by_id(db: Session, clock: ManualClock) -> None:
    for i in range(4):
        _audit(db, clock, action=f"same-ts-{i}")
    db.flush()
    p1 = list_audit(db, limit=2, before=None)
    p2 = list_audit(db, limit=2, before=(p1[-1].ts, p1[-1].id))
    assert len({r.id for r in p1 + p2}) == 4


def test_outbox_rolls_back_with_business_txn(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
) -> None:
    with pytest.raises(RuntimeError), session_factory.begin() as s:
        emit_event(s, event_type="e", payload={"a": 1}, now=clock.now())
        raise RuntimeError
    assert db.execute(text("select count(*) from outbox")).scalar_one() == 0


def test_emit_serializes_decimal_and_datetime(db: Session, clock: ManualClock) -> None:
    emit_event(
        db, event_type="e", payload={"p": Decimal("1.50"), "t": clock.now()}, now=clock.now()
    )
    db.flush()
    payload = db.execute(text("select payload_json from outbox")).scalar_one()
    assert payload == {"p": "1.5", "t": "2026-10-19T13:30:00Z"}


def test_dispatch_marks_delivered_and_skips_failed_handler(db: Session, clock: ManualClock) -> None:
    emit_event(db, event_type="good", payload={"n": 1}, now=clock.now())
    emit_event(db, event_type="bad", payload={"n": 2}, now=clock.now())
    emit_event(db, event_type="unhandled", payload={"n": 3}, now=clock.now())
    db.flush()
    delivered: list[tuple[str, dict[str, Any]]] = []

    def bad(event_type: str, payload: Any) -> None:
        raise RuntimeError("downstream down")

    handlers = {"good": lambda t, p: delivered.append((t, dict(p))), "bad": bad}
    assert dispatch_pending(db, handlers, now=clock.now()) == 2  # good + unhandled (logged)
    assert delivered == [("good", {"n": 1})]
    rows = dict(db.execute(text("select event_type, delivered_at is not null from outbox")).all())
    assert rows == {"good": True, "bad": False, "unhandled": True}
    assert dispatch_pending(db, handlers, now=clock.now()) == 0  # only 'bad' remains, still failing


def test_dispatch_respects_limit(db: Session, clock: ManualClock) -> None:
    for i in range(3):
        emit_event(db, event_type="x", payload={"i": i}, now=clock.now())
    db.flush()
    assert dispatch_pending(db, {"x": lambda t, p: None}, now=clock.now(), limit=2) == 2
