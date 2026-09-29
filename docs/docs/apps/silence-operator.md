# Silence operator

## Purpose

`kubernetes/apps/observability/silence-operator` runs Giant Swarm's `silence-operator`, which
turns `Silence` custom resources into Alertmanager silences so permanent mutes live in Git instead
of Alertmanager's own UI. `silences/` holds four cluster-wide Silences the observability stack
treats as permanent rather than incident-specific.

## Active silences

- **`ceph-network-packet-drops` mutes `CephNodeNetworkPacketDrops`.** Packet drops on the storage
  nodes' interfaces are transient and resolve on their own, but were firing about 20 alerts a day
  before this silence.
- **`etcd-high-commit-durations` and `etcd-member-communication-slow` mute the etcd commit-duration
  and heartbeat alerts.** The stock thresholds assume enterprise SSDs; this cluster's disks exceed
  them under fsync-heavy load without any correlated cluster problem.
- **`zeroscaler-hpa-maxed` mutes `KubeHpaMaxedOut` cluster-wide.** Every zeroscaler HPA runs at
  `maxReplicas: 1` and is permanently "maxed" whenever its NFS dependency is up, a pattern covered
  in [KB-024](../troubleshooting/kb/024-zeroscaler-nfs-hpa.md). The silence can't be scoped to just
  the zeroscaler apps: Alertmanager matches on the alert's own labels, and the stock
  `KubeHpaMaxedOut` rule carries only `alertname`, `namespace` and `horizontalpodautoscaler`, not
  the HPA object's `app.kubernetes.io/part-of` label. If a genuine, non-zeroscaler HPA is ever
  added to the cluster, tighten the matcher to a regular expression on the
  `horizontalpodautoscaler` label instead of dropping the silence.
- **`zfs-storage-memory` mutes `NodeMemoryHighUtilization` for the storage server.** ZFS's ARC
  deliberately uses all available RAM and releases it under memory pressure, so high utilization
  there is expected rather than a leak.
