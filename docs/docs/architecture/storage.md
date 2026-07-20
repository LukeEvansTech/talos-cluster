# Storage

The cluster uses three storage tiers, chosen per workload:

- **Rook-Ceph** (`ceph-block` StorageClass) — replicated block storage for stateful workloads that
  need durability and to move between nodes.
- **miroir** (`miroir-local` StorageClass) — loopfile-backed node-local volumes for workloads that
  want fast node-local storage and tolerate being pinned to a node. Unlike the retired OpenEBS
  hostpath, miroir enforces the requested PVC size (a real ext4 loopfile), so size volumes and
  VolSync caches deliberately.
- **NFS** (TrueNAS) — bulk storage (media, etc.) and a backup target.

## Choosing a tier

- Default to `ceph-block` for app state that must survive node loss and reschedule freely.
- Use `miroir-local` when the workload is latency-sensitive or explicitly node-local; remember it
  pins the pod and a single RWO claim cannot be mounted by pods on two nodes at once (Multi-Attach
  deadlocks show up on rollouts — co-locate the consumers).
- Use NFS for large shared datasets and as a VolSync destination.

## Backups

PVC backups are handled by VolSync — Kopia to NFS, Restic to a remote (R2) target — see
[Backups](../operations/backups.md) and the
[VolSync / Kopia migration](../migrations/volsync-kopia.md).
