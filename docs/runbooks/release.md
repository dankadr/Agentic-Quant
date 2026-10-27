# Release identity, deployment, and service roles

## Canonical deployment

- **Checkout:** one deployment checkout, on a tagged commit or the `main` revision that was
  reviewed. Do not deploy from a working tree with uncommitted changes.
- **Invocation:** from the checkout root, `make up`. It runs `scripts/init-secrets.sh`, then
  `TRADING_RELEASE_REVISION=<short sha> docker compose up -d --build`.
- **Image name:** `robinhood-trading:<short sha>`. The `:local` tag is only the fallback for an
  unversioned build (`TRADING_RELEASE_REVISION` unset).

## Verifying what is running

`GET /v1/system/status` (viewer role) returns a `release` object:

| Field | Meaning |
|---|---|
| `revision` | Git commit the image was built from (`unknown` if not injected) |
| `image` | Image reference injected at deploy (`unknown` if not injected) |
| `config_sha256` | SHA-256 over every `configs/*.yaml` file the process loaded |
| `schema_head` | Newest migration shipped with this code |
| `schema_applied` | Migration the database actually has (`null` if never migrated) |

A deployment is consistent when `schema_applied == schema_head` and the `revision` matches the
checkout being deployed. Before a rollout, compare the `config_sha256` against the value recorded
for that revision.

## Service roles

Each role claims only the job kinds assigned to it (`src/trading/jobs/kinds.py`).

| Service | Command | Holds executor lease | Job kinds | Broker code |
|---|---|---|---|---|
| `worker` | `trading worker` | yes | execution + operations | yes (only here) |
| `scheduler` | `trading scheduler` | scheduler lease | none (enqueues only) | no |
| `research` | `trading research-worker` | no | `agent_*` | no |
| `api` | `trading api` | no | none | no |

Because `research` holds no executor lease, a stalled model call cannot interrupt execution
heartbeats, ticks, or operator commands. Its health check verifies database reachability only; a
slow research job appears as a long-running row in `jobs`.

Market-data provider: the worker and the research worker both derive the provider name from the
same Alpaca data-key files (`alpaca-iex` when present, `replay` otherwise). Research scopes its data
by that name, so both roles read the same data. Both services mount the data keys in compose.

## Lease renewal

- The executor and scheduler leases are renewed on a background thread every 5 seconds (or every
  third of the TTL, whichever is shorter). Work does not delay renewal.
- Each running job's lease is renewed on its own thread (every third of the job lease). A job whose
  lease was reclaimed cannot record its result.
- If renewal fails for a whole TTL, or another holder takes the lease, the worker stops taking jobs
  and exits with code 3. Fenced writes in flight are rejected by the database.

## Rollback

Schema changes are additive. Roll back the code by redeploying the previous revision with
`TRADING_RELEASE_REVISION=<old sha> docker compose up -d --build`; do not downgrade the schema while
newer writers may still be running.
