# kube-prometheus-stack

## Purpose

`kube-prometheus-stack` runs Prometheus, Alertmanager and their operator for the cluster. It adds
the storage host's node and SMART metrics, patches one chart alert rule, and routes notifications
to Pushover and a Healthchecks.io dead man's switch.

## TargetDown exclusions

`postRenderers` patches the chart's generic `TargetDown` rule to exclude idle printers, two slow
SNMP walks, and the seedbox's relayed scrapes. The patch itself documents each exclusion; this page
covers the printer fix, the first of the three.

The Brother label printers power themselves off by design, so snmp-exporter's own
`SnmpTargetDown` already excludes `device_class="printer"`. `TargetDown` aggregates by job rather
than by target, so one sleeping printer out of five SNMP targets was 20%, above the rule's 10%
threshold, and fired anyway, defeating that exclusion and training whoever was on call to ignore
the one alert that would catch a real switch outage.

PromQL's `!=` matcher treats an absent label as a match, so `device_class!="printer"` drops only
targets explicitly labelled as printers rather than every target missing that label. Verified live
when the exclusion shipped: 173 `up` series, 169 kept, exactly the 4 printers excluded, with every
other job still in scope.

## NAS scrape configuration

The storage host is not a cluster node, so its node-exporter and smartctl-exporter metrics arrive
through two `ScrapeConfig` resources here rather than a ServiceMonitor.

The NAS keeps the cluster's `job="node-exporter"` name rather than a `truenas-*` one because 22
default kube-prometheus-stack node alert rules select that job: filesystem fill, RAID degraded,
bonding degraded, clock skew, and systemd unit failed, among others. This is the box the rest of
the estate depends on, and renaming the job would drop it out of all 22.

A duplicate `truenas-node` ScrapeConfig once scraped the same host under a different job name. Its
`node_uname_info` drop was completely ineffective, because this job was already publishing
`node_uname_info` for the same host, and unscoped ceph-mixin rules that join through that metric
picked up both copies: measured before the fix, the ceph join returned 178 filesystem series for
this host, 89 from each copy. The duplicate was removed. Before removing it, both of the other
unscoped ceph node alerts were checked against this box rather than assumed safe:

- `CephNodeRootFilesystemFull` keys on `mountpoint="/"`, which here is a real 17.1 GB filesystem at
  0.6% used, unlike FCOS's composefs `/`, but nowhere near the alert's threshold.
- `CephNodeInconsistentMTU` compares each device against the cluster-wide median for that device
  name.
- Of the 22 alert rules selecting `job="node-exporter"`, zero join through `node_uname_info`, so
  dropping it here costs this host no alerting and starves only the unscoped ceph rules.

The smartctl-exporter job name is load-bearing for a different reason: `truenas-exporter`'s
`prometheusrule.yaml` and its `truenas-zfs.json` dashboard hardcode `job="smartctl-exporter"` in
five places, so the drive-temperature alert and four dashboard panels depend on the NAS's drives
arriving under this exact job name. A duplicate `truenas-smartctl` ScrapeConfig was removed on
2026-08-09; this ScrapeConfig is the copy that was always feeding them.

## Alertmanager routing

The `CephHealthWarning` inhibit rule excludes both catch-all alert names from its own source match
rather than relying on Alertmanager's built-in two-sided matching, so source and target stay
disjoint by construction instead of depending on the matcher's default behaviour.
