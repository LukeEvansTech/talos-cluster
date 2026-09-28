# KB-145: satisfactory's VolSync backup covered the wrong PVC for 345 days

**Status:** Resolved (PR #3879, 2026-07-26). Both `ReplicationSource` objects now target
`sf-gamedata`.

## Symptom

Nothing. Both `ReplicationSource` objects for `satisfactory` reported healthy the entire time, and
no alert fired.

## Cause

`ks.yaml` already pulled in `components/volsync`, whose templates set `sourcePVC: ${APP}`, which
resolves to `satisfactory`, the empty 5Gi PVC the component itself creates. The HelmRelease actually
mounts `existingClaim: sf-gamedata` (30Gi, `./pvc.yaml`), the game's real save data. The backup was
archiving the empty placeholder PVC, and the 30Gi of save data was never covered.

## Fix

`kubernetes/apps/games/satisfactory/app/kustomization.yaml` patches both `ReplicationSource`
objects' `spec.sourcePVC` to `sf-gamedata`. The patch lives here rather than changing the shared
component's default, since 40+ other apps use that component and already have a PVC name matching
`${APP}`.

## Related

- PR #3879
