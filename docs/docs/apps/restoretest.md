# Restore test

`volsync-system/restoretest` proves that VolSync backups restore to readable data. A backup that
reports `Successful` only proves the mover ran; this test proves the data comes back.

## What it does

A weekly CronJob (Sunday 03:00) picks a quarter of the apps, by a hash of `namespace/app`, so each app
is tested about once a month. For each app and each repository (R2 restic and NFS Kopia) it:

1. Copies the app's live ReplicationDestination (`<app>-dst` or `<app>-nfs-dst`) as
   `rt-<app>[-nfs]-<run>`, with a fresh manual trigger; per-run names let a manual run overlap
   the weekly one. This is the same restore path a
   disaster recovery uses: mover restore, VolumeSnapshot, PVC populated from it.
2. Creates a PVC from it, with the app's capacity and storage class.
3. Mounts that PVC read-only in a verify pod running as the mover's user, reads every byte of every
   file, and runs `pragma quick_check` on every SQLite database (on a private copy, so a `-wal` is
   replayed as the app would replay it).
4. Deletes the scratch objects. A **failed** target's objects are kept for inspection; each run
   starts by deleting any scratch objects older than 7 hours, past the Job's 6-hour deadline, so no
   live run's objects are touched.

A target fails when the restore does not succeed, the volume does not mount (ceph-csi runs `fsck`
first), the volume is empty, a read returns an I/O error, or any SQLite check is not `ok`. A file
counts as SQLite if its header still shows SQLite's shape, so a damaged header is checked rather
than skipped. A database over 4GiB with a non-empty `-wal` fails as unverifiable, because it is
checked in place and an immutable read would skip the WAL; test such an app by hand. The Job
exits non-zero if any target fails. Files the verify user cannot read are counted as `denied`, not
failed, because the mover that backed them up ran as the same user.

Volumes over 50Gi are skipped and listed in the log; test them by hand (below).

## Alerts

- `VolsyncRestoreTestFailed`: the latest Job failed. Its log's `FAILED` lines name each target and
  the reason.
- `VolsyncRestoreTestStale`: no run has passed in 8 days.

## Running it by hand

Create a Job from the CronJob and override the environment:

```sh
kubectl -n volsync-system create job rt-manual --from=cronjob/restoretest --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].env += [{"name":"ONLY","value":"custom/sleeper,media/plex"}]' \
  | kubectl apply -f -
kubectl -n volsync-system logs -f job/rt-manual
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `ONLY` | unset | Comma-separated `ns/app`; ignores the shard and the size cap |
| `SHARD` | this ISO week mod `SHARDS` | A shard number, or `all` |
| `REPOS` | `r2,nfs` | Which repositories to test |
| `MAX_CAPACITY_GI` | `50` | Skip larger volumes unless named in `ONLY` |
| `CONCURRENCY` | `3` | Restores in flight at once |
| `KEEP_FAILED` | `true` | Keep a failed target's PVC and ReplicationDestination |

## Why it exists

On 2026-11-08, eleven apps moved into the `custom` namespace and their new PVCs restored from R2.
Five SQLite apps then read zeroed b-tree pages, sleeper's app logged `database disk image is
malformed` a minute after starting, and sleeper's volume later failed `fsck`. File sizes, volume
usage and HTTP checks all looked normal, so the damage surfaced an hour later through a metrics
endpoint, not through any check.

Restoring the same restic snapshots straight from R2 afterwards gave byte-identical, clean data,
including the backups taken *from* the damaged volumes after the restore. A repeat of the full
VolSync restore path into a scratch PVC was also clean. The data in the repositories was sound;
the damage appeared on the live restored volumes, and its cause is not yet known. The kernel logged
`ext4_dx_csum_verify` directory-checksum errors on one restored volume. Running the real restore
path every week, with a check that can fail, is how a recurrence gets caught.
