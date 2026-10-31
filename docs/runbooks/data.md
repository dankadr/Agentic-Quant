# Where the data lives, and how to keep it

All persistent data is on the host, in plain folders:

| Folder (default `./data`) | What it holds | Lost if removed |
|---|---|---|
| `postgres/` | the database: ledger, orders, fills, risk decisions, agent runs, research metadata, API keys | everything |
| `documents/` | original research files (immutable, content-addressed) | the originals; citations stop resolving |
| `prometheus/` | metrics history | metrics history only |

Set `TRADING_DATA_DIR` to put them on another disk. `scripts/init-secrets.sh` creates the folders.

## Rules

- Containers hold no data. Rebuilding, restarting or removing containers keeps the folders.
- **Never run `docker compose down -v`.** Nothing in this project uses named volumes for application
  data now, so `-v` only removes containers and networks. The smoke tests no longer use `-v`, and
  they write to a throwaway folder, never to `./data`.
- Stop the stack with `docker compose down` (or `make down`). Start it with `make up`.
- Deleting a folder is a restore, not a cleanup. Take a verified backup first (`scripts/backup.sh`).

## Linux only: Postgres ownership

The database container runs as uid 70. On Linux, once, after `init-secrets.sh`:

    sudo chown -R 70:70 data/postgres

On macOS Docker Desktop this is not needed.

## Verify persistence

    docker compose down && make up
    # the data is still there: same API keys, same ledger rows

## Backups

`scripts/backup.sh` reads the folders above and writes an encrypted bundle off the host. See
`operations.md` for the restore drill.
