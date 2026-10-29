"""Alpaca PAPER trading adapter. The only broker adapter that can reach a network.

Hard limits, enforced at construction and in every request:
- The base URL must be the paper trading host. The live host, any other host, and any redirect are
  refused. Redirects are never followed.
- Credentials are paper-account credentials, mounted into the execution service only.
- A client order id is sent with every submission. After a timeout the adapter reports
  ``BrokerTimeout``; it never resubmits. The execution manager then reconciles by client order id.

Endpoints and field names follow Alpaca's documented trading API. Unknown order statuses are not
guessed at: they raise ``BrokerTimeout``, so reconciliation decides, not this adapter.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from trading.brokers.base import (
    BrokerAccountState,
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.core.enums import OrderType, Side

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE_URL = f"https://{PAPER_HOST}"
_TIMEOUT_SECONDS = 10.0
_ORDER_LIMIT = 500
_MAX_PAGES = 200

_STATUS = {
    "new": BrokerOrderStatus.ACCEPTED,
    "accepted": BrokerOrderStatus.ACCEPTED,
    "pending_new": BrokerOrderStatus.ACCEPTED,
    "accepted_for_bidding": BrokerOrderStatus.ACCEPTED,
    "pending_cancel": BrokerOrderStatus.ACCEPTED,
    "pending_replace": BrokerOrderStatus.ACCEPTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "canceled": BrokerOrderStatus.CANCELED,
    "expired": BrokerOrderStatus.EXPIRED,
    "done_for_day": BrokerOrderStatus.EXPIRED,
    "rejected": BrokerOrderStatus.REJECTED,
}
_OPEN_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
                            "pending_replace", "accepted_for_bidding"})  # fmt: skip


class AlpacaConfigError(ValueError):
    """The adapter was configured for something other than the paper account."""


def _when(raw: object) -> datetime:
    text = str(raw).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dec(raw: object) -> Decimal | None:
    return None if raw is None or raw == "" else Decimal(str(raw))


def map_status(raw: str) -> BrokerOrderStatus:
    try:
        return _STATUS[raw]
    except KeyError:
        raise BrokerTimeout(f"UNMAPPED_STATUS:{raw}") from None


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool


class AlpacaPaperAdapter:
    def __init__(
        self,
        *,
        key_id: str,
        secret: str,
        base_url: str = PAPER_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        host = httpx.URL(base_url).host
        if base_url.rstrip("/") != PAPER_BASE_URL or host != PAPER_HOST:
            raise AlpacaConfigError("only the Alpaca PAPER host is permitted in this phase")
        self._client = httpx.Client(
            base_url=PAPER_BASE_URL,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,  # a redirect could point anywhere: refuse it, do not follow it
            transport=transport,
        )
        self._clock = clock

    def __repr__(self) -> str:
        return "AlpacaPaperAdapter(paper)"  # never includes credentials

    def close(self) -> None:
        self._client.close()

    # ---- transport -----------------------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BrokerTimeout("TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise BrokerUnavailable(type(exc).__name__) from exc
        if response.status_code in (301, 302, 303, 307, 308):
            raise BrokerUnavailable("REDIRECT_REFUSED")
        if response.status_code >= 500:
            raise BrokerUnavailable(f"HTTP_{response.status_code}")
        if response.status_code == 429:
            raise BrokerUnavailable("RATE_LIMITED")
        return response

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code in (401, 403):
            raise BrokerUnavailable("CREDENTIALS_REJECTED")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            message = response.json().get("message", "") if response.content else ""
            raise BrokerRejected(f"HTTP_{response.status_code}:{str(message)[:80]}")
        return response.json()

    # ---- orders --------------------------------------------------------------------------

    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrderState:
        body: dict[str, Any] = {
            "symbol": req.symbol,
            "qty": str(req.qty),
            "side": "buy" if req.side is Side.BUY else "sell",
            "type": "limit" if req.order_type is OrderType.LIMIT else "market",
            "time_in_force": req.time_in_force.value.lower(),
            "client_order_id": req.client_order_id,
        }
        if req.order_type is OrderType.LIMIT:
            if req.limit_price is None:
                raise BrokerRejected("LIMIT_PRICE_REQUIRED")
            body["limit_price"] = str(req.limit_price)
        return self._order_state(self._json(self._call("POST", "/v2/orders", json=body)))

    def get_order(self, client_order_id: str) -> BrokerOrderState | None:
        for raw in self._orders(status="all"):
            if raw.get("client_order_id") == client_order_id:
                return self._order_state(raw)
        return None

    def cancel_order(self, client_order_id: str) -> BrokerOrderState:
        state = self.get_order(client_order_id)
        if state is None:
            raise BrokerRejected("UNKNOWN_CLIENT_ORDER")
        if state.status not in (BrokerOrderStatus.ACCEPTED, BrokerOrderStatus.PARTIALLY_FILLED):
            return state  # already terminal: nothing to cancel
        response = self._call("DELETE", f"/v2/orders/{state.broker_order_id}")
        if response.status_code not in (200, 204, 207):
            self._json(response)  # raises for the refusal
        refreshed = self.get_order(client_order_id)
        if refreshed is None:
            raise BrokerTimeout("CANCEL_NOT_VISIBLE")
        return refreshed

    def list_open_orders(self) -> Sequence[BrokerOrderState]:
        return [self._order_state(raw) for raw in self._orders(status="open")]

    def _orders(self, *, status: str) -> list[dict[str, Any]]:
        """Every matching order, newest first, paged backwards until none remain.

        A submission that timed out may be far back in the list. Missing it would make
        reconciliation conclude the order never existed, so the pages are not capped at the first.
        """
        collected: list[dict[str, Any]] = []
        until: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, str] = {
                "status": status,
                "limit": str(_ORDER_LIMIT),
                "direction": "desc",
            }
            if until is not None:
                params["until"] = until
            page = list(self._json(self._call("GET", "/v2/orders", params=params)) or [])
            collected.extend(page)
            if len(page) < _ORDER_LIMIT:
                return collected
            until = str(page[-1]["created_at"])
        raise BrokerUnavailable("ORDER_HISTORY_TOO_LONG")

    def _order_state(self, raw: Mapping[str, Any]) -> BrokerOrderState:
        return BrokerOrderState(
            client_order_id=str(raw["client_order_id"]),
            broker_order_id=str(raw["id"]),
            status=map_status(str(raw["status"])),
            qty=Decimal(str(raw["qty"])),
            filled_qty=Decimal(str(raw.get("filled_qty") or 0)),
            avg_fill_price=_dec(raw.get("filled_avg_price")),
            updated_at=_when(raw.get("updated_at") or raw["created_at"]),
        )

    # ---- account, positions, fills ---------------------------------------------------------

    def get_account(self) -> BrokerAccountState:
        account = self._json(self._call("GET", "/v2/account"))
        positions = self._json(self._call("GET", "/v2/positions")) or []
        return BrokerAccountState(
            cash=Decimal(str(account["cash"])),
            positions={str(p["symbol"]): Decimal(str(p["qty"])) for p in positions},
            as_of=self._clock(),
        )

    def account_number(self) -> str:
        return str(self._json(self._call("GET", "/v2/account"))["account_number"])

    def list_fills(self, *, since: datetime | None = None) -> Sequence[BrokerFill]:
        params: dict[str, str] = {"direction": "asc", "page_size": "100"}
        if since is not None:
            params["after"] = since.isoformat()
        raw = self._json(self._call("GET", "/v2/account/activities/FILL", params=params)) or []
        fills = [
            BrokerFill(
                execution_id=str(a["id"]),
                client_order_id=str(a.get("client_order_id") or ""),
                broker_order_id=str(a["order_id"]),
                symbol=str(a["symbol"]),
                side=Side.BUY if str(a["side"]).lower() == "buy" else Side.SELL,
                qty=Decimal(str(a["qty"])),
                price=Decimal(str(a["price"])),
                fees=Decimal(
                    0
                ),  # Alpaca paper charges no commission; fees are estimated, not booked
                slippage_estimate=Decimal(0),
                ts=_when(a["transaction_time"]),
            )
            for a in raw
        ]
        return sorted(fills, key=lambda f: (f.ts, f.execution_id))

    # ---- capability ------------------------------------------------------------------------

    def asset(self, symbol: str) -> AssetInfo | None:
        raw = self._json(self._call("GET", f"/v2/assets/{symbol}"))
        if raw is None:
            return None
        return AssetInfo(
            symbol=symbol,
            tradable=bool(raw.get("tradable")) and raw.get("status") == "active",
            fractionable=bool(raw.get("fractionable")),
        )
