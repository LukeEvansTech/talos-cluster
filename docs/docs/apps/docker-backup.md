# Docker-estate backup alerting

## Purpose

`kubernetes/apps/observability/docker-backup` is a CR-only Kustomization: it owns five
PrometheusRule alerts and scrapes nothing itself. Several Docker hosts outside the cluster each run
their own backup job, push to an offsite destination, and publish run metrics through a
node-exporter textfile collector that each host's own scrape config in this repository covers.
This directory exists solely to alert on those metrics.

## Design

Every alert here pages: Alertmanager's root route sends every severity to Pushover, so `warning`
is not a quiet tier. Each alert is a boolean about something that either worked or did not, rather
than a threshold on a continuous signal. That mirrors a lesson from the seedbox rules
(`docs/docs/apps/seedbox.md`): a threshold calibrated against a live metric like iowait can drift
into the normal working band, while a boolean about success keeps meaning the same thing regardless
of how the system happens to be behaving right now.

The alerts fire on the absence of success, not the presence of errors, because that is how backup
systems actually fail: a removed container, an expired credential, or a script that never runs
writes no error event at all.

## Alert notes

- `DockerBackupStale` keeps alerting even after the backup container is gone, because it reads a
  textfile written to disk rather than a live process, and that file simply stops advancing.
- `DockerBackupMetricsMissing` uses four per-box `absent()` selectors rather than one bare
  `absent()`, because with three boxes reporting, a single bare `absent()` would only fire once all
  three had stopped, a case already covered by the other rules at a higher severity. A per-box
  selector still misses a narrower case: if a box's other tier keeps reporting while one tier goes
  silent, the box-level `absent()` never fires. `docs/docs/operations/hardening-backlog.md` (H-6)
  tracks closing that gap with per-box-and-tier selectors.
- No alert covers `restic_backup_duration_seconds` yet. The dataset is small enough today that a
  run takes seconds, so any duration threshold chosen now would be a guess; revisit if growth ever
  makes a slow-backup problem plausible.

See [KB-064](../troubleshooting/kb/064-docker-backup-frozen-nas-snapshot-silent-success.md) for the
incident behind the two source-freshness alerts.
