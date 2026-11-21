# Paper short rollout

Use this runbook after approval to release the short-capable code. Obtain separate approval to
enable new shorts. The internal paper simulator assumes borrow availability and omits borrow fees.
It reserves collateral in addition to the restricted sale proceeds.

## Prepare the release with shorts disabled

1. Integrate the reviewed commits into the current deployment branch, preserving any newer
   deployed work. Use a clean checkout of the reviewed integrated revision. Record the existing
   status from `GET /v1/system/status`, the image tag, and the deployment configuration. Confirm
   `configs/risk.yaml` has `allow_short: false` and the executor uses the internal paper simulator.
2. Rehearse the upgrade on an isolated restore using the current deployed schema. Confirm the
   migration graph applies revisions `0013` and `0014`, and that existing long quantities, basis,
   settled cash, and the independent paper venue balance are identical across the migration.
3. Schedule an off-session maintenance window. With an operator key, use
   `POST /v1/control/stop-entries` with `Idempotency-Key`, the current `expected_version`, and an
   audited reason. Wait for the command to finish and confirm pending entry orders are terminal.
   Reconcile before pausing. Send `POST /v1/control/pause` with a fresh key and version, then stop
   the writers and API from the current deployment checkout. Keep PostgreSQL available for backup.

   ```sh
   docker compose stop scheduler worker research heavy api
   ./scripts/backup.sh
   ```

   Record the encrypted off-host bundle. Run `./scripts/restore-drill.sh` from a separate Compose
   project backed by isolated PostgreSQL and a distinct `TRADING_DATA_DIR`. Confirm the drill
   restores the recorded bundle before proceeding. See [backup and restore operations](operations.md).

4. From the reviewed release checkout, use the existing deployment environment and data directory.
   Confirm the service list and the PostgreSQL data mount with `docker compose config`. Keep the
   original project name so the deployment continues to use the existing data. Build once, apply
   the owner-role migration, and start the app services only after it succeeds.

   ```sh
   ./scripts/init-secrets.sh
   ./scripts/check-data.sh
   export TRADING_RELEASE_REVISION="$(git rev-parse --short HEAD)"
   docker compose build migrate
   docker compose up -d --no-build --force-recreate migrate
   migration_container="$(docker compose ps -a -q migrate)"
   test "$(docker wait "$migration_container")" = 0
   docker compose logs migrate
   docker compose up -d --no-build
   ```

   Stop if the migration exits nonzero and inspect its logs while the application is paused.

## Acceptance probes

- `GET /v1/health/ready` returns `database: ok` and `executor_lease: held`.
  `GET /v1/system/status` with a viewer key reports `PAPER_ONLY`, the approved revision and
  configuration fingerprint, `schema_applied == schema_head` including `0014`, and
  `allow_short: false`. Confirm startup reconciliation is current and clean.
- Compare accounting cash, equity, open long quantities and basis to the pre-migration capture.
  `GET /v1/positions` keeps signed `qty` and adds `direction`, absolute `size` and `signed_qty`.
  With no shorts, short liability, collateral and restricted proceeds are zero.
- Rehearse both flag states on a separate simulator. Check a profitable and a losing short with
  partial and final covers, and a default-off entry rejection. With exposure present, verify equity
  includes the negative short market value and buying power excludes collateral, restricted proceeds
  and pending-order reservations. Check the size, entry, signed P&L, and short amounts in Overview
  and Trading. Orders, signals, fills and round trips identify `SELL to open` and `BUY to cover`.
- Verify a fresh tradable quote is required to cover at the close. A missing quote leaves the
  position and reservations visible and raises an incident. After reconciliation and normal
  readiness checks, use the usual `resume-paper` command only when the rollout window allows it.

## Enable or disable new shorts after approval

1. Stop entries and wait for the cancellation command and reconciliation. Keep covers available.
2. Create an operator-managed copy of `configs/risk.yaml` on the deployment host. Change only
   `allow_short` in that copy. Apply the same read-only file to every service that loads risk
   configuration through a Compose overlay. Compose can reuse the reviewed image.

   ```yaml
   x-paper-risk: &paper-risk
     volumes:
       - ${PAPER_RISK_FILE:?absolute path required}:/app/configs/risk.yaml:ro
   services:
     api: *paper-risk
     worker: *paper-risk
     scheduler: *paper-risk
     research: *paper-risk
     heavy: *paper-risk
   ```

   ```sh
   export PAPER_RISK_FILE=/absolute/path/to/reviewed-risk.yaml
   export PAPER_SHORTS_COMPOSE=/absolute/path/to/paper-shorts.compose.yml
   export TRADING_RELEASE_REVISION="$(git rev-parse --short HEAD)"
   docker compose -f docker-compose.yml -f "$PAPER_SHORTS_COMPOSE" config
   docker compose -f docker-compose.yml -f "$PAPER_SHORTS_COMPOSE" up -d --no-build \
     --force-recreate api worker scheduler research heavy
   ```

   Retain this overlay in subsequent deploy invocations while the reviewed policy is active.
   Confirm worker startup reconciliation, `allow_short` and the configuration fingerprint before
   resuming entries. After disabling, verify existing shorts can cover and that no resting
   short-entry orders remain.

3. If enablement behaves unexpectedly, stop entries and reapply the same procedure with
   `allow_short: false`. Retain the short-aware code and schema while shorts or short history are
   present. Keep an unresolved carry visible and reconcile it before reopening entries. Migrations
   guard against erasing short or corporate-action history. Coordinate any backup restore using the
   [recovery runbook](recovery.md).
