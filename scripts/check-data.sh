#!/usr/bin/env bash
# Preflight for `make up`. Refuses to start a stack over data that is missing or was never copied.
#
# A database folder is "initialised" once Postgres has written PG_VERSION there. Once a deployment
# has initialised, its folder must never be silently replaced by an empty database. Three cases:
#   1. initialised folder present                      -> OK; record the marker.
#   2. folder empty, marker present                    -> REFUSE: the data was lost or moved.
#   3. folder empty, old named volume still exists     -> REFUSE: copy the volume first.
#   4. folder empty, no marker, no old volume          -> fresh install, allowed.
# TRADING_ALLOW_FRESH_DATABASE=1 overrides case 3 only, and must be set deliberately.
set -euo pipefail
cd "$(dirname "$0")/.."
data_dir="${TRADING_DATA_DIR:-data}"
pg_dir="$data_dir/postgres"
marker="$data_dir/.initialised"
mkdir -p "$pg_dir"

if [ -f "$pg_dir/PG_VERSION" ]; then
  touch "$marker"
  echo "database present under $pg_dir"
  exit 0
fi

if [ -f "$marker" ]; then
  echo "REFUSING TO START: $marker exists but $pg_dir has no database." >&2
  echo "The data was moved or deleted. Restore it from a verified backup (docs/runbooks/data.md)." >&2
  exit 1
fi

if docker volume inspect robinhood-trading_pgdata >/dev/null 2>&1; then
  if [ "${TRADING_ALLOW_FRESH_DATABASE:-}" != "1" ]; then
    echo "REFUSING TO START: the old volume robinhood-trading_pgdata exists but $pg_dir is empty." >&2
    echo "Copy the old data first (stop the stack, then):" >&2
    echo "  docker run --rm -v robinhood-trading_pgdata:/src:ro -v \"$(cd \"$pg_dir\" && pwd)\":/dst alpine cp -a /src/. /dst/" >&2
    echo "Then: sudo chown -R 70:70 $pg_dir   (Linux only). Or, for a genuinely new database," >&2
    echo "set TRADING_ALLOW_FRESH_DATABASE=1 once." >&2
    exit 1
  fi
  echo "WARNING: starting a fresh database beside the old volume (override set)." >&2
fi

echo "fresh install: $pg_dir will be initialised on first start"
