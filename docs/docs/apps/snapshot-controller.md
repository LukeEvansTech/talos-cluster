# snapshot-controller

## Purpose

`kubernetes/apps/kube-system/snapshot-controller` runs the CSI external-snapshotter controller
that backs `VolumeSnapshot` and `VolumeSnapshotClass` resources for the cluster's storage.

## CRD lifecycle

The chart ships its CRDs under `crds/`, which Helm installs once and never upgrades or deletes.
This replaced an older postRenderer that stamped `helm.sh/resource-policy: keep` onto templated
CRDs from a previous chart, a workaround this chart's packaging no longer needs.

The trade-off is that Helm will not pick up CRD changes on its own. After bumping the chart's
`appVersion`, apply the matching CRDs by hand:

```bash
helm show crds oci://ghcr.io/home-operations/charts/snapshot-controller \
  | kubectl apply --server-side -f -
```
