import time

from sqlalchemy.exc import OperationalError

from trading.jobs.keepalive import Keepalive


def _wait(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_renews_until_stopped_and_is_not_lost() -> None:
    calls: list[int] = []

    def renew() -> bool:
        calls.append(1)
        return True

    keeper = Keepalive("t", renew, interval=0.01, ttl=1.0).start()
    assert _wait(lambda: len(calls) >= 3)
    keeper.stop()
    assert not keeper.lost.is_set()


def test_reports_loss_when_the_claim_is_gone() -> None:
    keeper = Keepalive("t", lambda: False, interval=0.01, ttl=1.0).start()
    assert _wait(keeper.lost.is_set)
    keeper.stop()


def test_transient_database_errors_are_tolerated_within_the_ttl() -> None:
    calls = {"n": 0}

    def renew() -> bool:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("select 1", {}, Exception("blip"))
        return True

    keeper = Keepalive("t", renew, interval=0.01, ttl=5.0).start()
    assert _wait(lambda: calls["n"] >= 4)
    keeper.stop()
    assert not keeper.lost.is_set()


def test_persistent_database_errors_declare_the_claim_lost_after_one_ttl() -> None:
    def renew() -> bool:
        raise OperationalError("select 1", {}, Exception("down"))

    started = time.monotonic()
    keeper = Keepalive("t", renew, interval=0.01, ttl=0.1).start()
    assert _wait(keeper.lost.is_set)
    assert time.monotonic() - started >= 0.1
    keeper.stop()


def test_stop_returns_promptly_even_with_a_long_interval() -> None:
    keeper = Keepalive("t", lambda: True, interval=30.0, ttl=60.0).start()
    started = time.monotonic()
    keeper.stop()
    assert time.monotonic() - started < 5.0
