"""Gates the Alpaca paper adapter must pass before any order can be sent.

The adapter stays disabled until: it is enabled in configuration, the paper account matches the
configured binding, the account is flat, and the broker is reachable. Each failed check is a
reason, so an operator sees all of them at once rather than fixing them one at a time.
"""

from dataclasses import dataclass

from trading.brokers.alpaca.adapter import AlpacaPaperAdapter, AssetInfo
from trading.brokers.base import (
    BrokerOrderRequest,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.core.enums import OrderType


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: tuple[str, ...]


def read_only_gate(
    adapter: AlpacaPaperAdapter,
    *,
    enabled: bool,
    expected_account: str | None,
    require_flat: bool,
) -> GateResult:
    """Read-only checks only. Nothing here places, cancels or changes an order."""
    reasons: list[str] = []
    if not enabled:
        reasons.append("DISABLED")
    if not expected_account:
        reasons.append("ACCOUNT_NOT_BOUND")
    try:
        if expected_account and adapter.account_number() != expected_account:
            reasons.append("ACCOUNT_MISMATCH")
        if require_flat:
            account = adapter.get_account()
            if any(qty != 0 for qty in account.positions.values()) or adapter.list_open_orders():
                reasons.append("NOT_FLAT")
    except (BrokerUnavailable, BrokerTimeout):
        reasons.append("BROKER_UNREACHABLE")
    except BrokerRejected as exc:
        reasons.append(f"BROKER_REFUSED:{exc.reason}")
    return GateResult(allowed=not reasons, reasons=tuple(reasons))


def capability_problems(
    req: BrokerOrderRequest,
    asset: AssetInfo | None,
    *,
    fractional_limit_verified: bool,
) -> list[str]:
    """Why this order cannot be sent to this asset. Empty means it may be sent."""
    if asset is None:
        return ["UNKNOWN_ASSET"]
    problems: list[str] = []
    if not asset.tradable:
        problems.append("NOT_TRADABLE")
    fractional = req.qty != req.qty.to_integral_value()
    if fractional and not asset.fractionable:
        problems.append("FRACTIONAL_NOT_SUPPORTED")
    if fractional and req.order_type is OrderType.LIMIT and not fractional_limit_verified:
        # Fractional LIMIT orders are only used once verified in the paper environment.
        problems.append("FRACTIONAL_LIMIT_NOT_VERIFIED")
    return problems
