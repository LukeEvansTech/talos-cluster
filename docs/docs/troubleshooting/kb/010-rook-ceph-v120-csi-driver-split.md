# KB-010: Rook-Ceph v1.20 CSI Driver Split Gotchas

**Status:** Resolved (PRs landed). Two runtime-only failures that `flate`/CI cannot catch.
Both render fine and only break on a live cluster. Read before any future Rook upgrade or
chart re-point.

## Background

Rook-Ceph v1.20 **splits CSI out of the operator chart**. The drivers are now managed by the
`ceph-csi-operator` via a **mandatory** separate `ceph-csi-drivers` HelmRelease
(`oci://ghcr.io/home-operations/charts-mirror/ceph-csi-drivers`). Without it, CSI fails on
missing service accounts. The Kustomization ordering is
`rook-ceph` → `ceph-csi-drivers` → `rook-ceph-cluster`.

## Symptom

- **Gotcha 1: in-use RBD storage breaks during the upgrade.** The RBD nodeplugin DaemonSet
  fails: `daemonset/rook-ceph.rbd.csi.ceph.com-nodeplugin FailedCreate: serviceaccount "rbd-nodeplugin-sa" not found`.
  New and existing RBD volumes stop attaching.
- **Gotcha 2: every VolSync `copyMethod: Snapshot` source goes out of sync.** A flood of
  `VolSyncVolumeOutOfSync` alerts (every `*-nfs` / `*-r2` ReplicationSource). CSI
  VolumeSnapshots are stuck `READYTOUSE=false` for hours; the VolSync controller logs
  `waiting for snapshot to be ready`. (NFS reachability is a red herring: `kopia-maint`
  jobs against the same repo keep completing.)

## Cause

### Gotcha 1: the charts-mirror driver name is unprefixed

The home-operations charts-mirror packages the **upstream** ceph-csi-operator chart, whose
`drivers.rbd.name` defaults to the **unprefixed** `rbd.csi.ceph.com`. Rook's own wrapper chart
defaults to the **namespace-prefixed** `rook-ceph.rbd.csi.ceph.com`, which is what this
cluster's `ceph-block` StorageClass and all in-use RBD PVs reference (`PV .spec.csi.driver`
is **immutable**). Deploying without the override creates a wrong-named driver, and the new
operator winds down the in-use one.

### Gotcha 2: `snapshotPolicy` defaults to `none`

The `ceph-csi-drivers` chart defaults `drivers.rbd.snapshotPolicy: "none"`, which **omits the
`csi-snapshotter` sidecar** from the RBD `ctrlplugin` deployment entirely. The
snapshot-controller still creates VolumeSnapshotContents but nothing calls `CreateSnapshot`,
so snapshots hang forever. Pre-v1.20 rook-managed CSI shipped the snapshotter by default, so
this regresses silently on migration.

## Fix

### Gotcha 1: pin the driver name to the prefixed form

Set, in the `ceph-csi-drivers` HelmRelease values:

```yaml
drivers:
  rbd:
    name: rook-ceph.rbd.csi.ceph.com
```

The pre-existing `Driver` CR is already annotated for the `ceph-csi-drivers` release, so it
adopts in place. After the rollout, the spurious unprefixed driver's SAs/RBAC are Flux-pruned,
but the cluster-scoped CSIDriver object is **not** GC'd by the operator. Delete it manually
once nothing references it:

```bash
kubectl delete csidriver rbd.csi.ceph.com
```

> Whether the override is needed depends on what your StorageClass/PVs reference. This cluster
> uses the **prefixed** provisioner, so the override is required; a cluster on the unprefixed
> form would not set it.

### Gotcha 2: enable the snapshotter, then clear the stale leader lease

Set, in `kubernetes/apps/rook-ceph/rook-ceph/csi-drivers/helmrelease.yaml`:

```yaml
drivers:
  rbd:
    snapshotPolicy: volumeSnapshot
```

Confirm the `ctrlplugin` pod gains a `csi-snapshotter` container. It then often **hangs at
`Attempting to acquire leader lease...`** because the snapshotter Lease was orphaned by the
pre-cutover pod that last held it. Delete the stale lease (a runtime leader-lock, **not**
GitOps-managed, safe):

```bash
kubectl -n rook-ceph delete lease external-snapshotter-leader-rook-ceph-rbd-csi-ceph-com
```

The snapshotter acquires instantly; backlogged source snapshots flip `ReadyToUse=true` within
minutes, then VolSync fires its movers in a thundering herd (many `Pending`/`Init`, drains
gradually). Out-of-sync alerts only clear once each mover **completes**. Note: a `v1.20.x`
operator point-bump does **not** fix Gotcha 2: `snapshotPolicy` is a chart value, independent
of operator version.

## References

- Rook CSI: <https://rook.io/docs/rook/latest/Storage-Configuration/Ceph-CSI/ceph-csi-drivers/>
- Rook drives the major Ceph upgrade itself (rolling, health-gated); the combined
  Rook + Ceph bump stayed `HEALTH_OK`. To stage Ceph separately, pin
  `cephClusterSpec.cephVersion.image`.
