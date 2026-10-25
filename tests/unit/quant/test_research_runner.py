from datetime import date, timedelta
from decimal import Decimal

from trading.quant.research_runner import cached_evaluator

D = Decimal
DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(6)]


def test_cached_evaluator_runs_each_candidate_once_and_slices_by_window() -> None:
    calls: list[str] = []

    def full_run(params):  # type: ignore[no-untyped-def]
        calls.append(str(params["x"]))
        return {d: D(params["x"]) / 1000 * (i + 1) for i, d in enumerate(DAYS)}

    ev = cached_evaluator(full_run, base_equity=D(1000))
    a = ev(DAYS[:2], {"x": 2})
    b = ev(DAYS[2:4], {"x": 2})
    c = ev(DAYS[:2], {"x": 3})
    assert calls == ["2", "3"]  # one full run per distinct parameter set
    assert a.daily_returns == (D("0.002"), D("0.004"))
    assert a.net_pnl == D("6")  # (0.002 + 0.004) x 1000
    assert b.daily_returns == (D("0.006"), D("0.008"))
    assert c.net_pnl == D("9")
    assert a.dates == tuple(DAYS[:2])


def test_a_day_the_run_never_covered_counts_as_zero_and_drawdown_is_tracked() -> None:
    def full_run(params):  # type: ignore[no-untyped-def]
        return {DAYS[0]: D("0.01"), DAYS[1]: D("-0.02")}

    ev = cached_evaluator(full_run, base_equity=D(1000))
    e = ev(DAYS[:3], {"x": 1})
    assert e.daily_returns == (D("0.01"), D("-0.02"), D(0))
    assert e.max_drawdown_pct > 0


def test_disk_cache_survives_a_restart_and_is_keyed_by_everything_that_matters(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from trading.quant.research_runner import disk_cached

    calls: list[dict[str, object]] = []

    def full_run(params):  # type: ignore[no-untyped-def]
        calls.append(dict(params))
        return {DAYS[0]: D("0.01"), DAYS[1]: D("-0.002")}

    fp = {"strategy": "orb_v1", "fixtures": "abc", "dates": [d.isoformat() for d in DAYS[:2]]}
    first = disk_cached(full_run, tmp_path, fp)
    a = first({"target_r": "2.0"})
    assert a == {DAYS[0]: D("0.01"), DAYS[1]: D("-0.002")}
    # a brand-new wrapper (a restarted process) finds the stored result
    second = disk_cached(full_run, tmp_path, fp)
    assert second({"target_r": "2.0"}) == a
    assert len(calls) == 1
    # different parameters, or a different fingerprint (other data), recompute
    second({"target_r": "1.0"})
    disk_cached(full_run, tmp_path, {**fp, "fixtures": "changed"})({"target_r": "2.0"})
    assert len(calls) == 3
    assert all(
        p.suffix == ".json" for p in tmp_path.iterdir()
    )  # no half-written temp files left behind
