# Backups

PVC data is backed up with **VolSync** — the NFS destination uses the **Kopia** mover and the remote
(R2) destination uses the **Restic** mover. Backups are opt-in per app via the `volsync` component.

## kopiur trial

A parallel backup path is being trialed alongside VolSync: the **kopiur** operator
(`kubernetes/apps/kopiur-system/kopiur`) takes CSI-snapshot-based, Kopia-native backups directly to a
dedicated NFS `ClusterRepository`, wired in per-app via the `components/kopiur/backup` component.
It's currently opted in on two apps — `apprise` and `atuin` — as a trial, running **alongside**, not
replacing, VolSync for those apps.

## Enabling backups for an app

- Add the `volsync` component to the app's `ks.yaml` `spec.components` (not also to
  `app/kustomization.yaml` — Flux applies `ks.yaml` components on top of the path build, so listing
  it in both double-applies).
- Set `VOLSYNC_CAPACITY` in the `ks.yaml` `postBuild.substitute` block to size the replication
  volume.

## Operating

- NFS backups run every 4 hours (`0 */4 * * *`); remote (R2) backups run nightly (`30 0 * * *`).
  Snapshots should reach a Succeeded state.
- Trigger snapshots for all PVCs on demand with `just kube snapshot`.
- For single-file SQLite databases, VolSync backs up the whole volume — see the
  [Autopulse](../apps/autopulse.md) page for that pattern.

See the [VolSync / Kopia migration](../migrations/volsync-kopia.md) for the move to the Kopia mover
and the repository layout.
