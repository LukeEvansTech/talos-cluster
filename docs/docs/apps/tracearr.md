# Tracearr

Media-server session and library analytics in the `media` namespace
([connorgallopo/Tracearr](https://github.com/connorgallopo/Tracearr)).

## Database

Tracearr builds its statistics on TimescaleDB: `sessions` and `library_snapshots` are hypertables
and five continuous aggregates (daily engagement, plays, bandwidth, library stats, content quality)
are refreshed by policy. Continuous aggregates are in Timescale's TSL-licensed code, so it needs
the full TimescaleDB build, not the Apache-only `timescaledb-oss` image CloudNativePG publishes.

- **Dedicated cluster.** `tracearr-postgres` (two instances, `media` namespace, Flux Kustomization
  `tracearr-database`) runs `timescale/timescaledb-ha`. TimescaleDB needs
  `shared_preload_libraries`, which on the shared `postgres18` would restart every tenant, so this
  is the dedicated-cluster case of the Postgres policy in
  [storage](../architecture/storage.md#postgresql-databases).
- **ImageCatalog.** The image is selected through an `ImageCatalog` with `major: 18`, because
  CloudNativePG rejects `imageName` when it cannot parse a major version from the tag
  (`pg18.6-ts2.30.2`). `postgresUID/GID: 1000` match the image's postgres user.
- **Operator access.** A CiliumNetworkPolicy lets the CNPG operator in `database` reach the
  instances; the clusterwide `allow-same-namespace` policy blocks it otherwise. Any dedicated
  cluster outside `database` needs the same.
- **Backups.** WAL archiving and daily base backups go to the `cnpg-tracearr` Garage bucket
  through the barman-cloud plugin (30-day retention), with its own key so the shared CNPG key
  never sits in `media`. The NAS mirror and R2 copy jobs pick the bucket up, and
  `CNPGBackupStale` alerts after 36 hours without a base backup. To restore, bootstrap a new
  Cluster with `recovery` from the `tracearr-postgres-garage` ObjectStore (resolved H-22).

## Migration from the shared cluster

The data moved from `postgres18` on 2026-10-09 with tracearr scaled to 0: `pg_dump -Fc` on the old
primary piped into `pg_restore --no-owner --no-acl --role=tracearr --single-transaction` on the new
one, run as the operator's superuser. Tracearr's own UI restore was not used: it drops and recreates
the `timescaledb` extension and calls `timescaledb_pre_restore()`, which need a superuser that the
app role is not. On first start Tracearr converted `sessions` and `library_snapshots` to hypertables
and built the continuous aggregates itself. The old database and role on `postgres18` were dropped the same day, once the new
cluster had a completed base backup.

## Upgrading TimescaleDB

Renovate tracks the image with regex versioning that keeps PostgreSQL 18 fixed and never
auto-merges. A new `ts` version changes the binaries but not the installed extension; the
extension belongs to the superuser, so neither CNPG nor Tracearr updates it. After the new image
has rolled out, run as the first command of a fresh session:

```bash
kubectl cnpg psql tracearr-postgres -n media -- -X -d tracearr -c 'ALTER EXTENSION timescaledb UPDATE'
```

A PostgreSQL major upgrade needs a dump and restore; use Tracearr's own backup and restore.

## Node rolls

The cluster has its own primary PodDisruptionBudget. Draining the node that hosts the primary
relies on CNPG switching over to the replica once the node is cordoned; if the replica is still
catching up from the previous node's reboot the drain can time out. Check
`kubectl -n media get cluster tracearr-postgres` shows both instances ready before rolling the next
node.
