# snapshot-controller

## Purpose

`kubernetes/apps/kube-system/snapshot-controller` runs the CSI external-snapshotter controller
that backs `VolumeSnapshot` and `VolumeSnapshotClass` resources for the cluster's storage.

## CRD lifecycle

The chart ships its CRDs under `crds/`. The root Kustomization
(`kubernetes/flux/cluster/ks.yaml`) patches every HelmRelease with `install.crds: CreateReplace`
and `upgrade.crds: CreateReplace`, so helm-controller server-side applies this chart's `crds/` on
every install and upgrade, keeping the CRDs current without a manual step.

This chart replaced an older one that templated its CRDs, guarded by a postRenderer stamping
`helm.sh/resource-policy: keep` onto them. Shipping CRDs in `crds/` instead of templating them
removed the need for that workaround.

A manual apply, `helm show crds oci://ghcr.io/home-operations/charts/snapshot-controller |
kubectl apply --server-side -f -`, is only the break-glass path for a HelmRelease that wedges
before completing its CRD pass. See
[KB-029](../troubleshooting/kb/029-chart-migration-deletes-keep-annotated-crds.md) for that
recovery procedure and the CRD-deletion incident it covers.
