# TrueNAS exporter

## Purpose

`kubernetes/apps/observability/truenas-exporter` provisions the dashboards and alert rules
for the storage host's metrics. The exporter setup, port inventory and the "metric reality"
of TrueNAS SCALE 25.10 are covered in
[TrueNAS monitoring setup](../operations/truenas-monitoring.md). This page covers what that
one doesn't: the replication-alert design and the scrapeconfig job-naming decision.

## Replication alert design

The graphite bridge exposes no replication series, so the signal comes from the host side
instead: a cron collector on the NAS writes `zfs_replication_*.prom`, and node-exporter's
textfile collector re-exposes it. The metrics are generic over every replication task, but
the off-site DR task is the load-bearing one.

The six alerts in `truenas-replication.rules` follow the same philosophy as
`docker-backup.rules`: alert on the absence of success, not the presence of errors. Every
rule pages, and each is expected to fire close to never.

A seed caveat runs through the design. The first full replication run reports
`state=RUNNING` and `last_success=0` for days, so `TrueNASReplicationStale`'s
`last_success > 0` guard stays quiet until the first completion, and
`TrueNASReplicationNeverSucceeded`'s 168h window is set well clear of that seed period.

Each alert covers one blind spot the others can't see:

- `TrueNASReplicationStale`: the off-site copy hasn't succeeded in 36h, a missed daily
  cycle plus slack. Guarded by `last_success > 0` (quiet through the seed and any disabled
  task) and `state != 1` (a long in-progress run isn't stale).
- `TrueNASReplicationFailing`: a terminal `ERROR`. zettarepl retries transient blips
  within a run, so the 15m fuse rides those out before paging.
- `TrueNASReplicationDisabled`: the DR task turned off, an absence-of-success mode with
  no error to catch. Scoped to the DR task's middleware id (`id="11"`) so an unrelated,
  deliberately-disabled task never pages.
- `TrueNASReplicationMetricsMissing`: the series vanishes entirely, which Stale can't
  see with no series to compare. Same `id="11"` scope, a 2h fuse; a full box outage is
  covered by other alerts.
- `TrueNASReplicationCollectorStale`: the collector cron stopped but the textfile still
  reads, so the series is present and frozen rather than missing. It attributes the fault
  to the collector after four missed 15-minute runs.
- `TrueNASReplicationNeverSucceeded`: the backstop for a task that never triggers or
  never finishes. Both keep `last_success` at 0 forever, which leaves Stale's and Failing's
  guards quiet indefinitely. The 168h window sits well past the seed period so a stuck task
  isn't mistaken for a still-seeding one.

`TrueNASReplicationDisabled` and `TrueNASReplicationMetricsMissing` are the only two rules
scoped to a specific task id. If the DR replication task is ever recreated in TrueNAS, it
gets a new middleware id, and both expressions need updating to match.

## Scrapeconfig job naming

The seedbox and the NUT appliance both use the estate's `<box>-*` job-naming convention
(see [seedbox](seedbox.md)), but this app's ScrapeConfigs keep the cluster's own
`node-exporter` and `smartctl-exporter` job names instead. That's a deliberate exception,
checked against two costs before it was kept:

- 22 of the default node-alert rules select `job="node-exporter"` (filesystem fill, RAID
  degraded, bonding degraded, clock skew, systemd unit failures and more). This host is the
  one the whole estate depends on, so losing that coverage to gain a naming convention was
  judged a bad trade.
- This app's own `prometheusrule.yaml` and `truenas-zfs.json` hardcode
  `job="smartctl-exporter"` in five places. Renaming the job would have silently blanked
  the drive-temperature alert and four dashboard panels.

This decision is also why the ScrapeConfigs for the NAS's node and SMART metrics don't live
in this app at all: they're scraped by `kube-prometheus-stack`'s own ScrapeConfig under
those same job names. See
[KB-038](../troubleshooting/kb/038-truenas-double-scraped-on-both-ports.md) for the incident
that established this.
