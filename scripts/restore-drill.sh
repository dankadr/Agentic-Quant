#!/usr/bin/env bash
# Restore drill: decrypt the newest backup, verify it, restore into a scratch database with
# execution disabled, and record that reconciliation is still required before re-arming.
#
# Required: TRADING_BACKUP_DEST, AGE_IDENTITY (path to the private key), and a scratch Postgres
# reachable as the "postgres" compose service. The live database is never touched.
set -euo pipefail

: "${TRADING_BACKUP_DEST:?set TRADING_BACKUP_DEST}"
: "${AGE_IDENTITY:?set AGE_IDENTITY}"
drill="$(mktemp -d)"
latest="$(ls -1t "$TRADING_BACKUP_DEST"/*.tar.age | head -n 1)"
age -d -i "$AGE_IDENTITY" -o "$drill/bundle.tar" "$latest"
tar -C "$drill" -xf "$drill/bundle.tar"
bundle="$(find "$drill" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

# Refuse a backup that does not match its own manifest before anything is restored from it.
docker compose run --rm -T -v "$bundle":/backup api admin verify-manifest --root /backup

docker compose exec -T postgres dropdb -U postgres --if-exists trading_drill
docker compose exec -T postgres createdb -U postgres trading_drill
docker compose exec -T postgres pg_restore -U postgres -d trading_drill < "$bundle/database.dump"

# Keep the drill inert: no executor, paused, and reconciliation required.
TRADING_DB_NAME=trading_drill docker compose run --rm -T api admin mark-restored \
  --reason "drill $(basename "$bundle")"

rm -rf "$drill"
echo "restore drill passed for $(basename "$bundle"); reconciliation remains required"
