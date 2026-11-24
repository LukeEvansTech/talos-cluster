# TrueNAS exporter

## Purpose

`kubernetes/apps/observability/truenas-exporter` provisions the dashboards and alert rules
for the storage host's metrics. The exporter setup, port inventory and the "metric reality"
of TrueNAS SCALE 25.10 are covered in
[TrueNAS monitoring setup](../operations/truenas-monitoring.md). This page covers what that
one doesn't: the replication-alert design, the SMART self-test alerts, the disk-temperature
baseline alert and the scrapeconfig job-naming decision.

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
  cycle plus headroom. Guarded by `last_success > 0` (quiet through the seed and any disabled
  task) and `state != 1` (a long in-progress run isn't stale).
- `TrueNASReplicationFailing`: a terminal `ERROR`. zettarepl retries transient blips
  within a run, so the 15m fuse rides those out before paging.
- `TrueNASReplicationDisabled`: the DR task turned off, an absence-of-success mode with
  no error to catch. Scoped to the DR task's middleware ID (`id="11"`) so an unrelated,
  deliberately-disabled task never pages.
- `TrueNASReplicationMetricsMissing`: the series vanishes entirely, which Stale can't
  see with no series to compare. Same `id="11"` scope, a 2h fuse; a full box outage is
  covered by other alerts.
- `TrueNASReplicationCollectorStale`: the collector cron stopped but the textfile still
  reads, so the series is present and frozen rather than missing. Its `> 3600` condition
  needs four missed 15-minute runs before it goes true, and the rule's own 15m `for` then
  holds it pending for a fifth, so it attributes the fault to the collector roughly 75
  minutes after it stops, not after four runs.
- `TrueNASReplicationNeverSucceeded`: the backstop for a task that never triggers or
  never finishes. Both keep `last_success` at 0 forever, which leaves Stale's and Failing's
  guards quiet indefinitely. The 168h window sits well past the seed period so a stuck task
  isn't mistaken for a still-seeding one.

`TrueNASReplicationDisabled` and `TrueNASReplicationMetricsMissing` are the only two rules
scoped to a specific task ID. If the DR replication task is ever recreated in TrueNAS, it
gets a new middleware ID, and both expressions need updating to match.

## SMART self-test alerts

smartctl-exporter reports a disk's overall health and its attributes but not its self-test
log, and the two can disagree for months. One NAS disk failed every self-test from June
2026 with `read failure` while its overall health still read PASSED, so
`SmartDeviceTestFailed` (which watches `smartctl_device_smart_status`) never fired. The only
notice was TrueNAS's own `SMARTFailedSelfTest` alert, which is a warning and goes out by email.

A cron collector on the NAS (`apps/monitoring/smart-selftest-metrics.sh` in
`truenas-apps-cr`) now reads each disk's self-test log hourly and writes
`smart_selftest.prom` for node-exporter's textfile collector, as the replication collector
does. The newest finished test counts; a test that is still running, was aborted or was cut
off by a reset is skipped. Disks with no self-test log (the SATADOMs and some NVMe drives) get
`smart_selftest_log_read 0` and no verdict. A disk whose log can't be read on one run, because
it is spun down or the command failed, keeps its previous verdict, so a failing disk's alert
does not clear on a bad read.

- `TrueNASDiskSelfTestFailed`: the newest completed self-test on a disk failed. It is keyed
  by serial and clears when a later test passes or the disk leaves the box. As a warning it
  repeats daily on Pushover while the disk stays in.
- `TrueNASSmartSelfTestCollectorStale`: the collector has not run for 3h (it runs hourly).
- `TrueNASSmartSelfTestMetricsMissing`: the series are gone for 3h.
- `TrueNASSmartSelfTestNoDevices`: a run read no disks, which would otherwise make a failing
  disk's alert resolve quietly.

br-storage runs the same collector (truenas-apps-br), and br-power republishes its series
as `br_smart_selftest_*` with the rest of br's exporters, so `BrStorageDiskSelfTestFailed`
covers the off-site box. br is up for about seven hours a night, too short for a LONG test to
finish on its disks, so it runs SHORT tests only, and a result shows up the night after.
`BrStorageSmartSelfTestStale` and `BrStorageSmartSelfTestMissing` watch the collector's own
timestamp, since the nightly exporter snapshot keeps advancing even if br's cron job stops,
and `BrStorageSmartSelfTestNoDevices` catches a run that read no disks.

On the main NAS the self-tests themselves are its SHORT and LONG cron jobs (on the 1st, 16th and 31st,
plus a monthly LONG), so a new failure shows up after the next scheduled test, not at once.

## Disk-temperature baseline alert

`TrueNASDiskTemperatureAboveBaseline` warns when an HDD runs more than 10 °C over its own
7-day median for an hour. It exists because the absolute rule (`> 60` °C) missed a real
cooling regression: on 2026-11-09 a BMC fan-mode change left the HDDs 22 °C over their weekly
median, at 58-60 °C, for two days without tripping it. In normal running each disk stays
within 3 °C of its median.

The rule selects `device=~"sd.*"` because the NVMe drives on the same job swing with load and
would make a median baseline noisy. It keys each disk by serial number, as the pending-sector
rules do, because `sdX` names can move after a reboot and would compare one disk against
another's history.

The baseline moves with the data, so a sustained hot spell drags the median up and the alert
stops firing after about 3.5 days. Past that point only the absolute rule is left, and it fires
above 60 °C: a disk that settles at 58-60 °C, as in the 2026-11-09 incident, goes unalerted
once the baseline clears. Treat a firing baseline alert as something to fix within those days.

## Scrapeconfig job naming

The seedbox and the NUT appliance both use the estate's `<box>-*` job-naming convention for
every exporter on the host (see [seedbox](seedbox.md)). This storage host's node and SMART
metrics are the deliberate exception: they keep the cluster's own `node-exporter` and
`smartctl-exporter` job names, scraped by `kube-prometheus-stack`'s own ScrapeConfig rather
than by this app (see below). That exception was checked against two costs before it was
kept:

- 22 of the default node-alert rules select `job="node-exporter"` (filesystem fill, RAID
  degraded, bonding degraded, clock skew, systemd unit failures and more). This host is the
  one the whole estate depends on, so losing that coverage to gain a naming convention was
  judged a bad trade.
- This app's own `prometheusrule.yaml` and `truenas-zfs.json` hardcode
  `job="smartctl-exporter"` in five places. Renaming the job would have silently blanked
  the drive-temperature alert and four dashboard panels.

See [KB-038](../troubleshooting/kb/038-truenas-double-scraped-on-both-ports.md) for the
incident that established this split of ownership.
