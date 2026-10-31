import time

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


def test_any_renewal_error_is_loss_immediately() -> None:
    """Fail closed: a single failed renewal stops the claim, with no tolerance window."""
    calls = {"n": 0}

    def renew() -> bool:
        calls["n"] += 1
        raise ConnectionError("blip")

    keeper = Keepalive("t", renew, interval=0.01, ttl=60.0).start()
    assert _wait(keeper.lost.is_set)
    keeper.stop()
    assert calls["n"] == 1


def test_a_renewal_that_overruns_the_ttl_is_loss() -> None:
    """A renewal that hangs past the claim's expiry cannot be trusted, even if it later succeeds."""

    def renew() -> bool:
        time.sleep(0.5)
        return True

    keeper = Keepalive("t", renew, interval=0.01, ttl=0.2).start()
    assert _wait(keeper.lost.is_set)
    keeper.stop()


def test_loss_is_declared_before_the_claim_can_expire() -> None:
    """With a renewal interval longer than the claim, the keeper must not sleep past expiry: it
    declares loss at the margin, before the TTL elapses, without attempting a late renewal."""
    calls: list[int] = []

    def renew() -> bool:
        calls.append(1)
        return True

    started = time.monotonic()
    keeper = Keepalive("t", renew, interval=5.0, ttl=0.4).start()
    assert _wait(keeper.lost.is_set)
    elapsed = time.monotonic() - started
    keeper.stop()
    assert calls == []
    assert elapsed < 0.4


def test_stop_returns_promptly_even_with_a_long_interval() -> None:
    keeper = Keepalive("t", lambda: True, interval=30.0, ttl=60.0).start()
    started = time.monotonic()
    keeper.stop()
    assert time.monotonic() - started < 5.0
