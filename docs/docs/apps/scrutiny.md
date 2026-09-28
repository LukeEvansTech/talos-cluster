# Scrutiny

## Purpose

`kubernetes/apps/observability/scrutiny` runs the Scrutiny UI and its embedded InfluxDB from the
`omnibus` image. It renders S.M.A.R.T. disk health from data that a separate `scrutiny-collector`
DaemonSet gathers on every node; this app never touches a physical disk itself.

## Why the in-pod collector stays off

`COLLECTOR_RUN_STARTUP` is false, and `COLLECTOR_CRON_SCHEDULE` is set to `0 0 30 2 *`, a date that
never occurs, so the image's own collector never runs. Collection already happens on the
`scrutiny-collector` DaemonSet, one pod per node with the host access `smartctl` needs. Keeping this
server's collector off means it never needs that access, so the pod stays unprivileged.

## InfluxDB 2.9 migration gate

Scrutiny v1.67.0 upgraded the omnibus image's embedded InfluxDB from 2.2 to 2.9.1, and the image
refuses to start on existing data without an explicit acknowledgement (upstream issue
Starosdev/scrutiny#662). Before the 1.67.0 upgrade landed on 2026-07-19, a manual VolSync snapshot
ran on both the NFS and R2 destinations (trigger `pre-influx29-*`), and both completed
successfully. `SCRUTINY_INFLUXDB_29_BACKUP_CONFIRMED` records that acknowledgement.

The gate only matters once: the image writes a persistent preflight marker after its first
confirmed boot past 1.67.0. The running image is now several releases past that point, so the
acknowledgement variable could be removed, but doing so would change rendered HelmRelease values,
which is outside a comments-only change.
