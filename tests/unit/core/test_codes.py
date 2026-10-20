from datetime import date

from trading.core.codes import RejectionCode
from trading.core.ids import make_client_order_id, new_id


def test_spec_codes_present() -> None:
    spec = {
        "MARKET_CLOSED",
        "STALE_MARKET_DATA",
        "INSUFFICIENT_CASH",
        "EXPOSURE_LIMIT",
        "DAILY_LOSS_LIMIT",
        "DRAWDOWN_LIMIT",
        "DUPLICATE_INTENT",
        "INVALID_TICK_SIZE",
        "UNSUPPORTED_ORDER",
        "RECONCILIATION_REQUIRED",
        "STRATEGY_DISABLED",
        "SIGNAL_EXPIRED",
        "RISK_STATE_UNKNOWN",
    }
    assert spec <= {c.value for c in RejectionCode}


def test_codes_equal_names_and_evaluation_order() -> None:
    names = [c.name for c in RejectionCode]
    assert all(c.value == c.name for c in RejectionCode)
    assert names[:3] == ["TRADING_PAUSED", "RISK_STATE_UNKNOWN", "RECONCILIATION_REQUIRED"]
    assert names[-1] == "MAX_OPEN_SYMBOLS"
    assert len(names) == 20


def test_client_order_id_format() -> None:
    assert make_client_order_id("orb_v1", date(2026, 10, 19), 1) == "paper-orb_v1-20261019-0001"


def test_new_id_unique() -> None:
    assert new_id() != new_id()
