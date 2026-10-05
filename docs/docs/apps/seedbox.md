# Seedbox monitoring

## Purpose

The seedbox is a remote host outside the cluster, provisioned from a separate private repository.
`kubernetes/apps/observability/seedbox` scrapes it over a Tailscale egress Service, probes its
public address, and alerts on its disks, traffic cap, containers, backups and torrent tooling.

## Scrape path and box probe

Every metric scrape uses one tailnet egress, which drops out through DERP relays and slows under
torrent iowait. So `up{instance="seedbox"}` reports on the monitoring path. The blackbox TCP probe
to the public address, `probe_success{job="seedbox-public"}`, reports whether the box is up.

- `SeedboxDown` uses the probe. The earlier rule used the tailnet scrape and false-paged about 43
  times a week.
- `SeedboxScrapePathDegraded` is the only in-band alert on scrape health, because the chart's
  generic `TargetDown` excludes `instance="seedbox"` (see the postRenderer in
  `kube-prometheus-stack`). If you narrow its `up{instance="seedbox"}` selector, the job you drop
  has no scrape alert at all.
- Its 20m `for` comes from measurement. In the 7 days to 2026-08-30 the path was partly degraded
  for 818 of 10,081 samples (8.1%), yet the rule fired for about 45 minutes in 14 days of
  retention. The degradation comes in short bursts, and 20m is longer than they last.
- The cluster runs no dead man's switch for the box. Its own Watchdog already covers Prometheus,
  Alertmanager and egress. The gap left is that the box sits in a different failure domain, so the
  box pings its own heartbeat check. A second pinger on that check would keep it green while either
  side was alive, which defeats the switch.

## Checking that rules still match

Several seedbox rules have matched zero series and could never fire. They selected label values
that disappeared when the box was rebuilt onto Fedora CoreOS on 2026-07-20, which renamed arrays
and mountpoints. After a change to the box's storage layout, an exporter upgrade, or a new
container label, invert each comparison and confirm the expression still returns series.

| Query                                                                                                         | Expected                                 |
| ------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `node_md_disks{job="seedbox-node",state="failed"} >= 0`                                                       | 3 series                                 |
| `node_filesystem_avail_bytes{job="seedbox-node",mountpoint=~"/var\|/var/mnt/seedbox"}`                        | 2 series                                 |
| `predict_linear(node_filesystem_avail_bytes{job="seedbox-node",mountpoint="/var/mnt/seedbox"}[7d], 10*86400)` | 1 series                                 |
| `count({job="seedbox-qbittorrent",__name__=~"qbittorrent_torrent_.+"})`                                       | unchanged by the upgrade                 |
| `count(container_state_status{container_label_<x>="..."})`                                                    | above zero before a rule relies on `<x>` |

mdadm renumbers any array it cannot match to `mdadm.conf`. That renumbering broke the old RAID
rule at the rebuild, so the current rules name no array.

## Disk fill

On the data array, `SeedboxDiskFillHigh` at 85% warns too late to act on. 85% of 21.6 TiB leaves
about 3.25 TiB free, which lasts about 3.2 days at the 1.0 TiB a day seen in the week to
2026-08-27. The fix is a qbit-manage retention cull, and a cull moves data to `.RecycleBin` on the
same filesystem, where it stays for 3 days. `SeedboxDiskFillProjected` therefore alerts on the
7-day trend over a 10-day horizon, which leaves time for a cull to free space.

A backtest on 2026-08-27 found the projection crossed zero at 22:29 on 2026-08-22 and stayed
negative for all 55 later samples. That was five days before anyone noticed the trend by hand. With
14 days of retention and a 7-day window, 2026-08-20 is the earliest point the rule can evaluate.

## Per-torrent cardinality

The qBittorrent exporter emits about 18 series per torrent. At 1,834 torrents that came to 36,427
series, 5.3% of the cluster's active series, and nothing read 30,905 of them. The series are keyed
on the torrent name, so every cull and every new download replaces part of the set.
`ENABLE_HIGH_CARDINALITY` does not control these base per-torrent gauges, so `servicemonitor.yaml`
drops them at ingest, which leaves 5,522 series.

- The relabelling drops named metrics and keeps the rest. Prometheus does not document whether
  `metric_relabel_configs` can remove the synthetic `up` and `scrape_*` series. A keep-list that
  removed `up` would silence `SeedboxQbittorrentExporterDown`, `seedbox:up:ratio` and the
  availability panel.
- `qbittorrent_torrent_states` is an aggregate whose `name` label holds the state, and a dashboard
  panel reads it. A regular expression written as `qbittorrent_torrent_.+` would remove it.
- `size_bytes`, `ratio` and `total_uploaded_bytes` stay because dashboard panels read them. If
  those readers go, drop these metrics too. `SeedboxDeadWeightHigh` reads
  `qbt_torrents_dead_weight_bytes` from the on-box collector instead, because these series cannot
  tell cross-seeds apart.

## One-shot containers

Restore and config init containers run, exit 0 and stay exited. Each carries the container label
`alerts.oneshot=true`, and `SeedboxContainerDown` excludes that label. Until 2026-08-23 a
name-matching regular expression did this job, and it missed an init named `-init-` instead of
`-restore-`. The exporter publishes no exit code or restart policy, so the intent has to be
declared on the container. A new one-shot container without the label will page, which is the
safer way to fail.
