# KB-029: Chart Migration Deletes Keep-Annotated CRDs (postRenderer Removal Races the Chart Swap)

**Status:** Incident during the snapshot-controller migration off the piraeus chart (#4187, 2026-08-10); fully recovered, no primary data loss. The mechanism is general: it applies to any migration off a chart that *templates* its CRDs when the protective postRenderer is removed in the same commit as the chart swap.

## Symptom

Minutes after merging a chart migration, the snapshot API vanishes and a rollback loop starts:

- The new controller pod crash-loops with a startup CRD check:

    ```text
    E... Failed to list v1 volumesnapshotclasses with error=the server could not
         find the requested resource (get volumesnapshotclasses.snapshot.storage.k8s.io)
    E... Exiting due to failure to ensure CRDs exist during startup: context deadline exceeded
    ```

- `kubectl get volumesnapshotclass` returns `the server doesn't have a resource type`, and `kubectl get crd | grep snapshot` comes back near-empty.
- The HelmRelease cycles: upgrade times out waiting on the crash-looping Deployment → `RemediateOnFailure` rolls back → retry. **Each retry deletes the CRDs again and each rollback recreates them**, so spot checks give contradictory answers depending on when you look.
- Every Flux Kustomization downstream of the app blocks on `dependency not ready` (here: volsync → rook-ceph-cluster → kube-prometheus-stack and friends).

The rollback-loop shape looks like KB-015, but the driver is missing CRDs, not image pulls.

## Cause

The migration PR changed two things in one commit: the OCIRepository (piraeus chart → home-operations chart) and the HelmRelease spec (removed the postRenderer that stamped `helm.sh/resource-policy: keep` onto the piraeus chart's *templated* CRDs). helm-controller reconciled those as **two upgrades, one second apart**:

1. The HelmRelease spec change reconciled first, against the **old** chart (the new OCI artifact had not been fetched yet). Re-rendering the old chart *without* the postRenderer meant the release manifest no longer carried the keep annotation — and Helm's three-way apply **stripped the annotation from the live CRD objects**.
2. One second later the artifact swap upgraded to the new chart, whose manifest contains no CRDs (it ships them in `crds/`). Helm diffed six CRDs out of the release, found no keep policy on them any more, and deleted them all — taking every `VolumeSnapshotClass`/`VolumeSnapshotContent` with them.

The subtlety: **Helm's keep decision reads the *previous release's stored manifest*, and the postRenderer output is part of that manifest.** The protection therefore dies at the first reconcile after the postRenderer's removal — one step *before* the chart swap it was guarding against.

## Fix

Recovery, in the order that worked:

1. **Stop the loop first**: `flux suspend hr snapshot-controller -n kube-system`. Until the HelmRelease is suspended, every retry re-deletes whatever you restore.
2. **Restore the CRDs** from the new chart's bundle, server-side:

    ```sh
    helm show crds oci://ghcr.io/home-operations/charts/snapshot-controller \
      | kubectl apply --server-side --force-conflicts -f -
    ```

3. With the CRDs present, the crash-looping pod starts and the in-flight upgrade completes on its own (`helm history` flips to `deployed`). Then `flux resume hr`.
4. **Recreate the cluster-scoped CRs the CRD deletion destroyed.** Helm will not self-heal deleted objects without drift detection, so force the owners:

    ```sh
    flux reconcile hr rook-ceph-cluster -n rook-ceph --force   # csi-ceph-* snapshot classes
    flux reconcile ks miroir-config -n miroir-system           # miroir snapshot class
    ```

5. Verify end to end: all three `VolumeSnapshotClass` objects back, downstream Kustomizations Ready, and one manually-triggered ReplicationSource (`spec.trigger.manual`) completes `Successful`.

Impact boundary: source PVCs and the Kopia repositories (the real backups) are untouched. What is lost is the cached `VolumeSnapshot`/`VolumeSnapshotContent` restore points, which the next scheduled VolSync cycles rebuild.

## Prevention

- **Annotate the live CRDs out-of-band before merging any migration off a CRD-templating chart:**

    ```sh
    kubectl annotate crd <each snapshot CRD> helm.sh/resource-policy=keep
    ```

    An annotation set by kubectl's field manager is not part of Helm's manifests, so no Helm apply will strip it — unlike the postRenderer, which lives and dies with the release.

- Keep a local copy of the new chart's CRDs (`helm show crds …`) *before* merging; recovery is one `kubectl apply` when the copy already exists.
- The end state is the safe one: with CRDs shipped in `crds/`, Helm installs them once and never upgrades or deletes them again. The cost is that CRD upgrades become a manual server-side apply after appVersion bumps (documented in the HelmRelease).
