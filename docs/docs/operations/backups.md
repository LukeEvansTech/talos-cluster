# Backups

PVC data is backed up with **VolSync**: the NFS destination uses the **Kopia** mover and the remote
(R2) destination uses the **Restic** mover. Backups are opt-in per app via the `volsync` component.

## kopiur trial

A parallel backup path is being trialed alongside VolSync: the **kopiur** operator
(`kubernetes/apps/kopiur-system/kopiur`) takes CSI-snapshot-based, Kopia-native backups directly to a
dedicated NFS `ClusterRepository`, wired in per-app via the `components/kopiur/backup` component.
It's currently opted in on two apps (`apprise` and `atuin`) as a trial, running **alongside**, not
replacing, VolSync for those apps.

## Enabling backups for an app

- Add the `volsync` component to the app's `ks.yaml` `spec.components` (not also to
  `app/kustomization.yaml`: Flux applies `ks.yaml` components on top of the path build, so listing
  it in both double-applies).
- Set `VOLSYNC_CAPACITY` in the `ks.yaml` `postBuild.substitute` block to size the replication
  volume.
- **Do not set `VOLSYNC_CACHE_CAPACITY` at all** unless the app needs *more* than the component
  default (currently 16Gi). Every app shares one Kopia repository, so the mover's `/cache` holds
  that repository's index and metadata rather than the app's own data: it does not scale down with
  a small app, and a trivial app peaks within 0.1Gi of a large one. Under-sizing it fills the cache
  volume and the mover dies on `no space left on device`
  ([KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md)).
  An override is a **cap**, not a floor, and that is the trap: 34 apps carried an 8Gi entry written
  in December 2025 to raise them off a 2Gi default, the default later moved past them, and on
  2026-09-11 all of them went critical while every app on the default was fine. Only `plex` (100Gi)
  and `jellyfin` (50Gi) should carry one, for content caches that genuinely exceed the shared
  index.
- **Keep regenerable data off the backed-up claim.** VolSync snapshots `spec.sourcePVC` and nothing
  else, so a cache or thumbnail directory mounted from its own PVC is excluded for free. Neither
  mover can exclude a path from a claim it is already backing up: restic has no exclusion field at
  all, and the Kopia mover's `policyConfig` writes the shared repository's *global* policy. `plex`
  is the worked example. See
  [Plex media bundles volume](../migrations/plex-media-bundles-volume.md).

## Operating

- NFS backups run every 4 hours (`0 */4 * * *`); remote (R2) backups run nightly (`30 0 * * *`).
  Snapshots should reach a Succeeded state.
- Trigger snapshots for all PVCs on demand with `just kube snapshot`.
- For single-file SQLite databases, VolSync backs up the whole volume. See the
  [Autopulse](../apps/autopulse.md) page for that pattern.
- **Deleting snapshots is a scoped operation on a shared repository.** All ~200 Kopia sources live
  in one `filesystem:///repository`, separated only by their `<source>@<namespace>` identity, so a
  purge must name that identity and pass manifest IDs, never `--all-snapshots-for-source`, never
  `policy set --global`, never `maintenance set`. Space also lags the delete by 24 to 48 hours
  behind Kopia's safety gates. The
  [Plex media bundles volume](../migrations/plex-media-bundles-volume.md) runbook has the worked
  procedure. The restic (R2) side needs no manual pruning at all: `pruneIntervalDays: 14` plus
  `retain: {daily: 30}` reclaims on its own.

See the [VolSync / Kopia migration](../migrations/volsync-kopia.md) for the move to the Kopia mover
and the repository layout.

## Restoring

Each app has two `ReplicationDestination` objects: `<app>-nfs-dst` (Kopia, from the NAS) and
`<app>-dst` (restic, from R2). Flux reconciles both, so **suspend the Kustomization before touching
one** or the trigger you bump gets reverted on the next reconcile:

```console
$ flux suspend ks <app> -n flux-system
$ TRIGGER="restore-$(date +%s)"
$ kubectl patch replicationdestination <app>-nfs-dst -n <ns> --type=merge \
    -p "{\"spec\":{\"trigger\":{\"manual\":\"$TRIGGER\"}}}"
```

`status.latestImage` is already populated from the destination's original creation-time
`restore-once` run, so its mere presence proves nothing. Watching for it to just be *set* can
recreate the PVC from a stale, pre-existing image while the new restore is still running. Wait for
`status.lastManualSync` to equal `$TRIGGER` (VolSync only updates it once the requested sync
completes) before recreating the app's PVC from `status.latestImage` and running
`flux resume ks <app> -n flux-system`:

```console
$ kubectl get replicationdestination <app>-nfs-dst -n <ns> \
    -o jsonpath='{.status.lastManualSync}'
```

Destinations were frozen at their creation-time values until 2026-08-22. See
[KB-031](../troubleshooting/kb/031-volsync-restore-destinations-never-updated.md) for what that
broke and the field-manager conflict to check for.
