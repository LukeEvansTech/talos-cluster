# KB-064: Docker-estate backup silently copied a frozen NAS snapshot for four nights

**Status:** Resolved (#4151).

## Symptom

For four nights from 2026-08-05, the NAS's Docker-estate backup ran and reported success while the
data it read never changed. Every `docker-backup.rules` alert stayed green throughout. The problem
was found by hand, not by an alert.

## Cause

The NAS backs up a fixed-name ZFS snapshot that a separate cron job refreshes shortly beforehand,
not the live filesystem tree. When that cron refresh silently failed, restic kept re-reading the
same old snapshot on every run and exited 0, so the existing stale-backup and failed-run alerts had
nothing to catch: the backup genuinely succeeded, just against a source that had stopped changing.

## Fix

Added two PrometheusRule alerts on metrics the refresh script now publishes:

- `DockerBackupSourceStale` (`time() - restic_snapshot_source_last_success_timestamp_seconds >
129600`, 30m, critical) catches a timestamp that stops advancing after the refresh has run at
  least once; the subtraction only fires once the metric already has a series to go stale.
- `DockerBackupSourceRefreshFailing` (`restic_snapshot_source_refresh_ok == 0`, 15m, warning)
  catches a refresh that ran and failed, since the script traps `EXIT` and writes `0` on every
  failure path. If the refresh cron has never run at all, neither metric has a series yet;
  `DockerBackupMetricsMissing`'s `absent()` arm catches that case after 2h instead.

## How to recognise fast

A backup that reads from a snapshot or a cached copy rather than a live source can succeed and
exit 0 even after whatever keeps that snapshot current has stopped working. A backup-success alert
only proves the backup step ran; it says nothing about whether its source was fresh. Any two-stage
pipeline shaped like this needs its own freshness check on the upstream stage, not just a success
check on the downstream one.

## References

- Fix: #4151.
