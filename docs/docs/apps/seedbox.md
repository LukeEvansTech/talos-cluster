# Seedbox monitoring

## Purpose

The seedbox is a remote host outside the cluster. `kubernetes/apps/observability/seedbox` scrapes it
over a Tailscale egress Service, probes its public address, and alerts on its disks, traffic cap,
containers, backups and the torrent tooling that runs on it. The host itself is provisioned from a
separate private repository.

## Two paths, two meanings

Every metric scrape rides one tailnet egress, which blips through DERP relays and slows under
torrent iowait. `up{instance="seedbox"}` therefore measures the **monitoring path**, not the box.
The blackbox TCP probe to the public address (`probe_success{job="seedbox-public"}`) is the box-up
anchor.

- `SeedboxDown` keys on the probe. The previous rule keyed on the tailnet scrape and false-paged
  about 43 times a week.
- `SeedboxScrapePathDegraded` is the only in-band alert on scrape health: the chart's generic
  `TargetDown` excludes `instance="seedbox"` (postRenderer in `kube-prometheus-stack`). Narrowing
  its `up{instance="seedbox"}` selector drops a job out of alerting entirely.
- Its 20m `for` is measured: over the 7 days to 2026-08-30 the path was partially degraded for 818
  of 10,081 samples (8.1%), yet the rule fired for about 45 minutes across 14 days of retention.
  The degradation is short bursts, and 20m sits above them.
- There is no cluster-side dead man's switch. The cluster's own Watchdog already covers
  Prometheus, Alertmanager and egress; the remaining gap is the box being in a different failure
  domain, so the box pings its heartbeat check itself. A second pinger on the same check would keep
  it green whenever either side lived, defeating the switch.

## Verifying rules are live

Several seedbox rules have been **structurally dead**: they selected label values that stopped
existing (the 2026-07-20 rebuild onto Fedora CoreOS renamed arrays and mountpoints), matched zero
series, and could never fire. After any change to the box's storage layout, an exporter bump, or a
new container label, invert each comparison and confirm the expression still returns series.

| Check                                                                                                         | Expected                          |
| ------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `node_md_disks{job="seedbox-node",state="failed"} >= 0`                                                       | 3 series                          |
| `node_filesystem_avail_bytes{job="seedbox-node",mountpoint=~"/var\|/var/mnt/seedbox"}`                        | 2 series                          |
| `predict_linear(node_filesystem_avail_bytes{job="seedbox-node",mountpoint="/var/mnt/seedbox"}[7d], 10*86400)` | 1 series                          |
| `count({job="seedbox-qbittorrent",__name__=~"qbittorrent_torrent_.+"})`                                       | stable after a bump               |
| `count(container_state_status{container_label_<x>="..."})`                                                    | non-zero before a rule uses `<x>` |

Arrays at the rebuild: `md125` is the 23.8 TB RAID5 data array at `/var/mnt/seedbox`, `md126` is
`/boot`, `md127` is `/var` (also mounted at `/etc`, `/sysroot` and the ostree deploy path). mdadm
renumbers arrays it cannot match to `mdadm.conf`, which is what killed the old RAID rule, so the
rules never name an array.

## Disk fill

`SeedboxDiskFillHigh` at 85% cannot stand alone on the data array. 85% of 21.6 TiB leaves about
3.25 TiB; at the ~1.0 TiB/day fill rate seen in the week to 2026-08-27 that is about 3.2 days. The
remedy, a qbit-manage retention cull, only moves data to `.RecycleBin` on the same filesystem, which
holds it for 3 days. `SeedboxDiskFillProjected` keys on the 7-day trend over a 10-day horizon so a
cull still has time to land.

Backtest on 2026-08-27: the projection crossed zero at 22:29 on 2026-08-22 and stayed negative for
55 of 55 later samples, five days before the trend was spotted by hand. With 14 days of retention
and a 7-day window, 2026-08-20 is the earliest evaluable point.

## Per-torrent cardinality

The qBittorrent exporter emits about 18 series per torrent. At 1,834 torrents that was 36,427
series, 5.3% of the cluster's active series, and 30,905 were read by nothing. Series key on the
torrent name, so every cull and grab churns the set. `ENABLE_HIGH_CARDINALITY` does not govern the
base per-torrent gauges, so `servicemonitor.yaml` drops them at ingest (to 5,522 series).

- It is a drop-list. Whether Prometheus exempts the synthetic `up` and `scrape_*` series from
  `metric_relabel_configs` is undocumented, and a keep-list that ate `up` would silence
  `SeedboxQbittorrentExporterDown`, `seedbox:up:ratio` and the availability panel.
- `qbittorrent_torrent_states` is an aggregate (its `name` label is the state) that feeds a panel;
  a regular expression written as `qbittorrent_torrent_.+` would eat it.
- `size_bytes`, `ratio` and `total_uploaded_bytes` stay because live panels and
  `SeedboxDeadWeightHigh` read them. Drop them too if those consumers go.

## Container one-shots

Restore and config inits run, exit 0 and stay exited. They carry the container label
`alerts.oneshot=true`, which `SeedboxContainerDown` excludes. A name-matching regular expression did this job until
2026-08-23 and missed an init named `-init-` rather than `-restore-`. Intent cannot be derived from
the exporter, which publishes no exit code or restart policy. A new one-shot without the label pages,
the safe failure direction.
