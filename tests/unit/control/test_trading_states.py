from datetime import UTC, datetime

import pytest

from trading.control.state import RiskState, SystemState, TradingState, blocks_order
from trading.execution.manager import _gate_reason

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


def _state(trading: TradingState, risk: RiskState = RiskState.NORMAL) -> SystemState:
    return SystemState(
        mode="PAPER_ONLY",
        trading_state=trading,
        risk_state=risk,
        risk_reason=None,
        version=1,
        updated_at=NOW,
        last_reconciled_at=None,
    )


@pytest.mark.parametrize(
    ("trading", "is_entry", "blocked"),
    [
        (TradingState.PAUSED, True, True),
        (TradingState.PAUSED, False, True),  # pause freezes everything, sells included
        (TradingState.PAPER_RUNNING, True, False),
        (TradingState.PAPER_RUNNING, False, False),
        (TradingState.EXIT_ONLY, True, True),  # no entries...
        (TradingState.EXIT_ONLY, False, False),  # ...but protective sells continue
        (TradingState.FLATTENING, True, True),
        (TradingState.FLATTENING, False, False),
    ],
)
def test_blocks_order_matrix(trading: TradingState, is_entry: bool, blocked: bool) -> None:
    assert blocks_order(trading, is_entry=is_entry) is blocked


def test_execution_gate_reports_why_entries_are_refused() -> None:
    assert _gate_reason(_state(TradingState.PAUSED), is_entry=False) == "TRADING_PAUSED"
    assert _gate_reason(_state(TradingState.EXIT_ONLY), is_entry=True) == "ENTRIES_DISABLED"
    assert _gate_reason(_state(TradingState.FLATTENING), is_entry=True) == "ENTRIES_DISABLED"


def test_execution_gate_lets_sells_through_in_exit_states() -> None:
    assert _gate_reason(_state(TradingState.EXIT_ONLY), is_entry=False) is None
    assert _gate_reason(_state(TradingState.FLATTENING), is_entry=False) is None


def test_risk_state_still_blocks_exits_when_unknown() -> None:
    """Exit states permit sells only if the risk state permits trading at all."""
    state = _state(TradingState.FLATTENING, RiskState.UNKNOWN)
    assert _gate_reason(state, is_entry=False) == "RISK_STATE_UNKNOWN"
