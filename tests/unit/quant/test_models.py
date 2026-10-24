from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trading.core.enums import Side
from trading.quant.models import SignalProposal

T = datetime(2026, 10, 19, 14, 45, tzinfo=UTC)
D = Decimal


def make(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        strategy_id="orb_v1", strategy_version="1.0.0", symbol="SPY", side=Side.BUY, generated_at=T,
        expires_at=T + timedelta(minutes=1), market_snapshot_id=uuid4(), target_notional_usd=D("100.00"),
        stop_price=D("575.10"), limit_price=D("576.00"), reason_codes=("ORB_BREAK", "VOLUME_CONFIRM"),
    )  # fmt: skip
    base.update(kw)
    return SignalProposal(**base)


def test_valid_signal_matches_the_spec_schema() -> None:
    s = make()
    assert s.target_notional_usd == D("100.00")
    assert isinstance(s.signal_id, UUID)
    dumped = s.model_dump(mode="json")
    assert dumped["generated_at"] == "2026-10-19T14:45:00Z"
    assert dumped["reason_codes"] == ["ORB_BREAK", "VOLUME_CONFIRM"]


def test_signal_id_is_deterministic_and_distinguishes_signals() -> None:
    snap = uuid4()
    assert make(market_snapshot_id=snap).signal_id == make(market_snapshot_id=snap).signal_id
    assert make().signal_id != make(symbol="QQQ").signal_id
    assert make().signal_id != make(side=Side.SELL, stop_price=None).signal_id


@pytest.mark.parametrize(
    "bad",
    [
        {"target_notional_usd": D("0")},
        {"target_notional_usd": D("-1")},
        {"expires_at": T},  # no time to live
        {"expires_at": T - timedelta(seconds=1)},
        {"expires_at": T + timedelta(minutes=6)},  # TTL cap is 5 minutes
        {"limit_price": D("0")},
        {"stop_price": D("0")},
        {"stop_price": D("576.00")},  # a long's stop must be below its limit
        {"reason_codes": ()},
        {"symbol": ""},
        {"generated_at": datetime(2026, 10, 19, 14, 45)},  # noqa: DTZ001  naive
    ],
)
def test_invalid_signals_are_rejected(bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises((ValidationError, ValueError)):
        make(**bad)


def test_floats_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make(target_notional_usd=100.0)


def test_signals_are_immutable() -> None:
    with pytest.raises(ValidationError):
        make().symbol = "QQQ"  # type: ignore[misc]


def test_exit_signal_needs_no_stop() -> None:
    s = make(side=Side.SELL, stop_price=None, reason_codes=("TARGET",))
    assert s.side is Side.SELL
