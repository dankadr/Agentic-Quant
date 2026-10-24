from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.agents.macro_features import compute_macro_features
from trading.data.models import Bar

D = Decimal
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


def bars(symbol: str, closes: list[str], volume: str = "1000") -> tuple[Bar, ...]:
    out = []
    prev = D(closes[0])
    for i, c in enumerate(closes):
        close = D(c)
        out.append(Bar(symbol=symbol, interval="1m", ts=OPEN + timedelta(minutes=i), open=prev,
                       high=max(prev, close), low=min(prev, close), close=close, volume=D(volume),
                       provider="t", source_seq=None))  # fmt: skip
        prev = close
    return tuple(out)


def test_a_steady_rally_is_efficient_and_broad() -> None:
    rally = [str(100 + i * 0.1) for i in range(40)]
    data = {s: bars(s, rally) for s in ("SPY", "QQQ", "IWM", "AAPL")}
    f = compute_macro_features(data, index_symbols=("SPY", "QQQ", "IWM"), min_bars=30)
    assert f is not None
    spy = f.indices["SPY"]
    assert spy.return_pct > 0
    assert spy.efficiency > D("0.95")  # nearly every minute moved the same way
    assert f.breadth_above_vwap == D(1)
    assert f.bars == 40


def test_chop_has_low_efficiency_and_neutral_breadth() -> None:
    chop = ["100.0", "100.2"] * 20
    data = {s: bars(s, chop) for s in ("SPY", "QQQ", "IWM")}
    f = compute_macro_features(data, index_symbols=("SPY", "QQQ", "IWM"), min_bars=30)
    assert f is not None
    assert f.indices["SPY"].efficiency < D("0.1")
    assert f.indices["SPY"].realized_vol_bps > 0


def test_too_few_bars_gives_no_features() -> None:
    data = {s: bars(s, ["100", "101"]) for s in ("SPY", "QQQ", "IWM")}
    assert compute_macro_features(data, index_symbols=("SPY", "QQQ", "IWM"), min_bars=30) is None


def test_missing_index_data_gives_no_features() -> None:
    data = {"SPY": bars("SPY", [str(100 + i) for i in range(40)])}
    assert compute_macro_features(data, index_symbols=("SPY", "QQQ", "IWM"), min_bars=30) is None


def test_features_are_numbers_only() -> None:
    data = {s: bars(s, [str(100 + i * 0.1) for i in range(40)]) for s in ("SPY", "QQQ", "IWM")}
    f = compute_macro_features(data, index_symbols=("SPY", "QQQ", "IWM"), min_bars=30)
    assert f is not None
    dumped = f.model_dump(mode="json")
    assert set(dumped) == {"bars", "breadth_above_vwap", "indices"}
    assert set(dumped["indices"]["SPY"]) == {
        "return_pct",
        "realized_vol_bps",
        "efficiency",
        "range_pct",
    }
