# Storage

The cluster uses four storage tiers, chosen per workload:

- **Rook-Ceph** — replicated storage for stateful workloads that need durability and to move
  between nodes. Two StorageClasses: `ceph-block` (RBD, RWO — the default for app state) and
  `ceph-filesystem` (CephFS, RWX — for volumes shared across pods/nodes, e.g. the llmkube shared
  model cache).
- **miroir** (`miroir-local` StorageClass) — loopfile-backed node-local volumes for workloads that
  want fast node-local storage and tolerate being pinned to a node. Unlike the retired OpenEBS
  hostpath, miroir enforces the requested PVC size (a real ext4 loopfile), so size volumes and
  VolSync caches deliberately.
- **NFS** (TrueNAS) — bulk storage (media, etc.) and a backup target.
- **Garage** (S3) — self-hosted S3-compatible object storage in the `storage` namespace, reached at
  `http://garage.storage.svc.cluster.local:3900` (region `us-east-1`, path-style). It replaced
  Rook's RGW/`CephObjectStore` and serves object-storage consumers such as CloudNativePG's
  barman-cloud backups and apps needing an S3 bucket.

## Choosing a tier

- Default to `ceph-block` for app state that must survive node loss and reschedule freely; use
  `ceph-filesystem` only when a volume genuinely needs RWX.
- Use `miroir-local` when the workload is latency-sensitive or explicitly node-local; remember it
  pins the pod and a single RWO claim cannot be mounted by pods on two nodes at once (Multi-Attach
  deadlocks show up on rollouts — co-locate the consumers).
- Use NFS for large shared datasets and as a VolSync destination.
- Use Garage when an app wants S3, provisioning the bucket and access key with the `/garage` CLI
  inside `garage-0` first.

## Backups

PVC backups are handled by VolSync — Kopia to NFS, Restic to a remote (R2) target — see
[Backups](../operations/backups.md) and the
[VolSync / Kopia migration](../migrations/volsync-kopia.md). A parallel `kopiur` operator trial
(CSI-snapshot-based Kopia backups on two apps) runs alongside VolSync; see Backups for details.
