# KB-038: TrueNAS host metrics scraped twice under two job names

**Status:** Resolved (#4150).

## Symptom

Every node-exporter and smartctl-exporter series from the storage host arrived twice, under
two different job labels. A query or panel scoped to exactly one job label (`node-exporter`
alone, say) only ever matched its own copy and read correctly. An unscoped query, or one
whose matcher spanned both job values, counted or fired for this host twice.

## Cause

`truenas-exporter` carried its own `truenas-node` and `truenas-smartctl` ScrapeConfigs,
added on the premise that "the NAS is the only Docker box in the estate with no host metrics
at all." That premise was never checked. `kube-prometheus-stack`'s own ScrapeConfig had
already been scraping this host's node-exporter (`:9100`) and smartctl-exporter (`:9633`)
under the cluster's own `node-exporter` and `smartctl-exporter` job names, from before
`truenas-exporter` existed.

The duplication was not just double-counting. Only the `truenas-node` copy dropped
`node_uname_info` to keep the unscoped `ceph-mixin` rules from adopting this host, so the
unfiltered `node-exporter` copy kept publishing it and those rules adopted the host anyway.
The drop protected nothing while both copies existed.

## Fix

Removed the `truenas-node` and `truenas-smartctl` ScrapeConfigs (#4150), after checking that
nothing else in the repository referenced those job names. The `node_uname_info` drop moved
to the surviving `node-exporter` ScrapeConfig, where it actually takes effect.

The surviving copies keep the cluster's own `node-exporter` / `smartctl-exporter` job names
rather than moving to the estate's `<box>-*` convention used by the seedbox and the NUT
appliance. Two costs were checked before that call, not assumed:

- 22 of the default node-alert rules select `job="node-exporter"` (filesystem fill, RAID
  degraded, bonding degraded, clock skew, systemd unit failures, and more). This host is the
  one the whole estate depends on, so losing that coverage to gain a naming convention was a
  bad trade.
- `truenas-exporter`'s own `prometheusrule.yaml` and `truenas-zfs.json` dashboard hardcode
  `job="smartctl-exporter"` in five places. Renaming the job would have silently blanked the
  drive-temperature alert and four dashboard panels.

## How to recognise fast

A panel or alert for this host reading roughly double its real value, or two ScrapeConfigs
covering the same port on the same host under different job names, is this pattern. Before
adding a per-app ScrapeConfig for a host-level metric, check whether
`kube-prometheus-stack`'s own ScrapeConfig (or any other observability app) already scrapes
that port under a different job name.

## References

- Fix: #4150.
- The ScrapeConfigs this replaced were added in #3961.
- [TrueNAS exporter](../../apps/truenas-exporter.md#scrapeconfig-job-naming) for the
  job-naming decision this incident produced.
