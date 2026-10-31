# Alpaca PAPER broker: read-only check and enablement

Status: **adapter built and tested against recorded-shape responses. Not connected to a live paper
account. Paper acceptance has not been run.**

## What the adapter can do

- Talks only to `paper-api.alpaca.markets`. Any other host is refused at construction. Redirects are
  refused, never followed.
- Submits orders with a client order id. After a timeout it reports `BrokerTimeout` and does **not**
  resubmit; the execution manager's reconciliation decides what happened.
- Finds any order by client order id by paging back through the full order history.
- Reads the account, positions, asset tradability and fractionability, and FILL activities.
- Parses `trade_updates` messages. Fills are recorded once, keyed by execution id, whether they came
  from the stream or from REST.

## Gates before any order can be sent

1. `DISABLED` until `TRADING_ALPACA_PAPER_ENABLED` is true.
2. `ACCOUNT_NOT_BOUND` / `ACCOUNT_MISMATCH`: the paper account number must equal the configured
   binding. A deployment bound to one account cannot quietly use another.
3. `NOT_FLAT`: no positions and no open orders at the start.
4. `BROKER_UNREACHABLE` / `BROKER_REFUSED`.
5. Per-symbol capability: tradable, and fractional orders only where the asset is fractionable.
   **Fractional LIMIT orders stay blocked** until `TRADING_ALPACA_FRACTIONAL_LIMIT_VERIFIED` is set,
   which must happen only after the paper environment has verified that behaviour.

## Read-only check (safe to run)

```
trading admin alpaca-readonly-check
```

It places, cancels and changes nothing. It prints the gate result and each universe symbol's
capability, and exits non-zero unless everything passes. Credentials are read from the files named by
`TRADING_ALPACA_PAPER_KEY_ID_FILE` and `TRADING_ALPACA_PAPER_SECRET_FILE`, which must be mounted into
the execution service only.

## Not yet done

- The execution tick still drives the internal simulator. The broker-neutral refactor (simulator
  advancement separated from execution and reconciliation) is not done, so the adapter is not wired
  into the live loop.
- The `trade_updates` WebSocket connection is not implemented; only its message parser and
  de-duplication are.
- Capital wiring: the risk engine still uses the internal ledger's equity. The lower-of-strategy-and-
  broker rule exists as a tested function (`trading.brokers.capital`) but is not yet applied.
- Corporate actions and broker-side fee handling are not modelled; fees are estimated, not booked.
- The activity identifier used as the execution id must be checked against the paper account.
