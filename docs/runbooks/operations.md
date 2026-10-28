# Operations: dashboard, alerts, backups, restore

## Dashboard

- Open `https://<tailnet-host>/ui` (served through `tailscale serve`; the API binds loopback).
- Sign in with an API key. The key is exchanged for a **30-minute** server-side session. The browser
  holds an opaque HttpOnly cookie (SameSite=Strict, Secure). Keys are never stored in the browser.
- Every form that changes state carries a CSRF token. A forged request changes nothing.
- Viewers read everything. Operators can acknowledge incidents.
- All text is escaped. Research documents can contain hostile markup; it is shown as text.

## Alerts

- Critical or warning conditions open **one** incident each (`incidents`), queue **one** notice,
  and queue **one** recovery notice when cleared. Repeated detection changes nothing.
- The watchdog runs every minute on the research/operations worker, not on the executor. It checks:
  executor lease missing, reconciliation required, market data stale or incomplete, a flatten that
  has stalled, and repeated research failures.
- Notices go to the one private Telegram chat in `TRADING_TELEGRAM_CHAT_ID`, using the token in
  `secrets/telegram_bot_token`. Each notice has six attempts with capped backoff, then is marked
  FAILED and shown on the dashboard. Nothing is retried forever.
- Only the incident title and detail are sent. Never a report, a payload, or a credential.
- **Setup:** create a bot, paste its token into `secrets/telegram_bot_token`, and set
  `TRADING_TELEGRAM_CHAT_ID`. Until both are set, notices stay queued and visible on the dashboard.
- Trading never waits for Telegram. Notices are sent from the research/operations worker only.
- Manual: `trading admin raise-incident --key <k> --severity CRITICAL --title ...` and
  `trading admin resolve-incident --key <k>`.

## Backups

- `scripts/backup.sh` writes one encrypted bundle per run to `TRADING_BACKUP_DEST`: a `pg_dump`
  (custom format), the document store, and a `MANIFEST.json` of sizes and SHA-256 for every file.
  The whole bundle is encrypted with `age` to `TRADING_BACKUP_RECIPIENT` before it leaves the host.
- A failed run raises a CRITICAL `backup_failed` incident. A later successful run clears it.
- Retention target: 7 daily and 4 weekly. Prune the destination according to that policy; the
  script does not prune on its own yet.
- The destination must be **off-host**. The decryption key must not live on the same host.

## Restore drill (run regularly, and before any real restore)

1. `scripts/restore-drill.sh` decrypts the newest bundle and **verifies its manifest first**. A
   truncated, substituted or partly copied bundle is refused here.
2. The database is restored into `trading_drill`, a scratch database. The live database is untouched.
3. The drill runs `admin mark-restored`, which leaves trading **paused** and sets
   **reconciliation required**. A restored database can predate broker fills.
4. Before any real restore is re-armed, an operator reconciles orders, fills, activities and
   positions against the broker, then clears the reconciliation state.

## Limits

- An on-host watchdog cannot report that its own host is down. Complete host failure needs a
  monitor on a **second** host, pointed at `/v1/health/live`. This is not yet provisioned.
- The backup script depends on `age`, `docker` and `pg_dump` on the host, and has been reviewed but
  not yet run against a live deployment.
