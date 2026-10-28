#!/usr/bin/env bash
# Encrypted off-host backup: database dump, document store, and a checksum manifest.
#
# Required environment:
#   TRADING_BACKUP_DEST       off-host destination (a mounted path or an rsync target)
#   TRADING_BACKUP_RECIPIENT  age public key; only its holder can decrypt the backup
# Any failure raises a CRITICAL incident (backup_failed) and exits non-zero.
set -euo pipefail

: "${TRADING_BACKUP_DEST:?set TRADING_BACKUP_DEST}"
: "${TRADING_BACKUP_RECIPIENT:?set TRADING_BACKUP_RECIPIENT}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$(mktemp -d)"
bundle="$work/$stamp"

on_failure() {
  docker compose run --rm -T api admin raise-incident --key backup_failed \
    --severity CRITICAL --title "Backup failed" --detail "backup $stamp did not complete" || true
  rm -rf "$work"
}
trap on_failure ERR

mkdir -p "$bundle"
# 1. Database: a custom-format dump taken from a consistent snapshot.
docker compose exec -T postgres pg_dump -U postgres -Fc trading > "$bundle/database.dump"
# 2. Documents: the immutable blob store, copied read-only.
docker run --rm -v robinhood-trading_docstore:/src:ro -v "$bundle":/out alpine \
  tar -C /src -cf /out/documents.tar .
# 3. Manifest over exactly what is in the bundle.
docker compose run --rm -T -v "$bundle":/backup api admin write-manifest --root /backup >/dev/null
# 4. Encrypt the whole bundle before it leaves the host.
tar -C "$work" -cf "$work/$stamp.tar" "$stamp"
age -r "$TRADING_BACKUP_RECIPIENT" -o "$TRADING_BACKUP_DEST/$stamp.tar.age" "$work/$stamp.tar"
trap - ERR
rm -rf "$work"
docker compose run --rm -T api admin resolve-incident --key backup_failed >/dev/null || true
echo "backup $stamp written to $TRADING_BACKUP_DEST"
