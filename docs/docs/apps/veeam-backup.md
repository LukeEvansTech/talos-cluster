# Veeam backup monitoring

## Purpose

`kubernetes/apps/observability/veeam-backup` is a CR-only app: a `ScrapeConfig` and a
`PrometheusRule`, no HelmRelease. The Veeam B&R server is a Windows VM outside the cluster and
publishes its own metrics, so there is nothing here to health-check; the cluster is only in the
alerting path. The design mirrors `docker-backup.rules`, which covers the same pattern for the
Docker boxes.

## Where the metrics come from

`windows_exporter` runs on the Veeam server itself, provisioned by the separate
`LukeEvansTech/veeam-config` repository (`ansible/monitoring.yml`). Besides the usual OS metrics it
serves a textfile collector, and that collector is where the `veeam_backup_*` series come from. A
scheduled task on the box (`veeam-config`, `powershell/Write-VeeamBackupMetrics.ps1`) parses event
190 (`Backup job 'X' finished with Success|Warning|Failed`) out of the "Veeam Backup" Windows event
log every 10 minutes and writes it into node-exporter-style textfiles:

| Series                                                   | Meaning                                       |
| -------------------------------------------------------- | --------------------------------------------- |
| `veeam_backup_last_success_timestamp_seconds{veeam_job}` | Last time the job finished Success or Warning |
| `veeam_backup_last_run_timestamp_seconds{veeam_job}`     | Last time the job finished, any result        |
| `veeam_backup_last_result_code{veeam_job}`               | `0` Success, `1` Warning, `2` Failed          |

## Alert design

Every rule alerts on the absence of a successful run rather than on an error event, because the
2026-08 outage that motivated this app (see below) produced no error at all, just a schedule that
stopped advancing. `VeeamBackupStale` is the load-bearing alert: 36 hours tolerates one missed
nightly run plus a 12-hour buffer, so a single transient failure self-heals without paging, and it
keeps working even while the box is dead, since Prometheus holds the last scraped value until the
staleness window expires. `VeeamBackupMetricsMissing` picks up from there once that value goes
stale, covering the case the first rule cannot see: the series disappearing outright because the
exporter, the textfile or the scheduled task is gone.

`VeeamBackupMetricsMissing` uses a bare `absent()` rather than a per-label selector, unlike
`docker-backup.rules`, because there is exactly one box and one exporter here, so there is no set of
labels to distinguish. Its 2-hour fuse and `warning` severity are deliberately soft: a genuine box
outage already pages at higher priority through `VeeamServerGuestDown` (`vmware-exporter` rules) and
`LanProbeFailed` (the blackbox TCP probe on the Veeam REST port), both of which live in other app
directories. This rule exists only for the box-up-but-metric-gone case those two cannot see.

Every alert here pages: Alertmanager routes every severity to Pushover, so `warning` is not a quiet
tier. Each rule is written as a boolean that should fire approximately never, not as a threshold
tuned against normal variation.

## The incident behind this app

`VeeamBackupStale`, `VeeamBackupMetricsMissing`, `VeeamWindowsExporterDown` and their sibling alerts
in `vmware-exporter` and `blackbox-exporter-lan` all exist because of one outage:
[KB-067](../troubleshooting/kb/067-veeam-server-bsod-silent-outage.md), where the Veeam server
crashed and sat unnoticed for 12 days.
