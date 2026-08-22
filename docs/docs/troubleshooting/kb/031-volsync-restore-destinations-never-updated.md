# KB-031: VolSync Restore Destinations Silently Frozen at Creation-Time Values

**Status:** Resolved. The `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent` label was removed from both
`ReplicationDestination` templates in `kubernetes/components/volsync/`, so Flux now reconciles all
198 of them instead of only creating them. Backups were never affected — this only ever broke
restores.

## Symptom

Nothing. That is the whole problem.

Every `ReplicationSource` is healthy, `lastSyncTime` is current, the Kopia and restic repositories
are fine, and no alert fires. The breakage only appears the day someone actually needs a restore,
which is the worst possible day to discover it.

Read the live values against what Git renders:

```console
$ kubectl get replicationdestination -A -o json \
  | jq -r '.items[] | [.metadata.name,
      (.spec.kopia // .spec.restic).capacity,
      (.spec.kopia // .spec.restic).cacheCapacity,
      (.spec.kopia // .spec.restic).cacheStorageClassName] | @tsv'
plex-nfs-dst     200Gi   100Gi   ceph-block
wizarr-nfs-dst   5Gi     2Gi     ceph-block
hermes-dst       5Gi     10Gi    openebs-hostpath
```

Three separate failures were live at once:

| Drift | Count | What a restore does |
| --- | --- | --- |
| `cacheStorageClassName: openebs-hostpath` | 42 | Hangs forever — that StorageClass was deleted with OpenEBS |
| `capacity` smaller than the data | 7 | Fails to provision; `plex` offered 200Gi for 290G of data |
| `cacheCapacity` below the 8Gi floor | 163 | ENOSPC on `/cache` — the KB-030 failure, during the restore |

## Cause

Both destination templates carried an SSA opt-out:

```yaml
metadata:
  name: "${APP}-nfs-dst"
  labels:
    kustomize.toolkit.fluxcd.io/ssa: IfNotPresent
```

`IfNotPresent` tells kustomize-controller to **create the object if it is absent and never update it
again**. Each destination therefore froze at whatever `VOLSYNC_CAPACITY` /
`VOLSYNC_CACHE_CAPACITY` happened to be on the day the app was first deployed. Every later change —
the Kopia migration's move to `miroir-local`, the OpenEBS decommission, the KB-030 8Gi floor, every
`VOLSYNC_CAPACITY` bump — landed on the `ReplicationSource` and was silently dropped on the
destination.

The label exists upstream so that a hand-bumped `spec.trigger.manual` is not reverted mid-restore.
That is a real concern, but it was buying one convenience at the cost of every restore path in the
cluster quietly rotting.

## Fix

Remove the label from both templates and let Flux own the objects:

- `kubernetes/components/volsync/nfs/replicationdestination.yaml`
- `kubernetes/components/volsync/remote/replicationdestination.yaml`

**Restores now need the Kustomization suspended first**, or Flux will revert the trigger you just
bumped:

```console
$ flux suspend ks <app> -n flux-system
$ kubectl patch replicationdestination <app>-nfs-dst -n <ns> --type=merge \
    -p '{"spec":{"trigger":{"manual":"restore-'"$(date +%s)"'"}}}'
# ... restore, then:
$ flux resume ks <app> -n flux-system
```

Note the quoting. One destination in this cluster was found holding the **literal** string
`restore-$(date +%s)`, because the shell never expanded it inside single quotes.

## Two traps when rolling this out

**Field-manager conflicts do not force themselves.** That hand-patched destination had
`spec.trigger` owned by the `kubectl-patch` field manager, so Flux's server-side apply returns:

```text
Error from server (Conflict): Apply failed with 1 conflict:
  conflict with "kubectl-patch" using volsync.backube/v1alpha1: .spec.trigger.manual
```

The Flux `Kustomization` does not set `spec.force`, so that one app wedges while the other 197
apply cleanly. Find them before merging — `kubectl get` hides this unless you ask:

```console
$ kubectl get replicationdestination -A -o json --show-managed-fields \
  | jq -r '.items[] | select(.metadata.managedFields[]?.manager
      | startswith("kubectl")) | .metadata.namespace + "/" + .metadata.name'
default/forgejo-dst
```

Clear it with a one-off `--server-side --field-manager=kustomize-controller --force-conflicts`
apply, which hands ownership back.

**Reconciling changes `spec.trigger.manual`, which fires one sync.** Any destination whose
`status.lastManualSync` does not match the rendered `restore-once` runs a restore as soon as Flux
owns it. This is safe: the templates set `copyMethod: Snapshot` and no `destinationPVC`, so the
mover restores into a temporary volume and snapshots it — the app's live PVC is never touched. It
costs a mover run and refreshes a stale `latestImage`.

## Verification

```console
$ kubectl get replicationdestination -A -o json \
  | jq -r '[.items[] | (.spec.kopia // .spec.restic).cacheStorageClassName] | unique'
["miroir-local"]
```

No destination should report `openebs-hostpath`, and no `cacheCapacity` below `8Gi`.

## Related

- [KB-030](030-volsync-kopia-cache-pvc-too-small.md) — the same ENOSPC, on the backup side
- [Backups](../../operations/backups.md) — the `8Gi` cache floor and the restore procedure
