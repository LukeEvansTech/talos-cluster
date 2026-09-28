# KB-145: satisfactory's VolSync backup may still target the wrong PVC

**Status:** Open. PR #3879 (2026-07-26) repointed both `ReplicationSource` objects from
`satisfactory` to `sf-gamedata`, believing `sf-gamedata` held the save data. Mount paths and the
image's own documentation say the reverse: this needs re-investigation before anyone relies on
either PVC's backup for a restore.

## What the mounts actually are

`kubernetes/apps/games/satisfactory/app/helmrelease.yaml` mounts two claims:

- `config` (`existingClaim: satisfactory`) at `/config`.
- `server-cache` (`existingClaim: sf-gamedata`) at `/config/gamefiles`, a subdirectory of the first
  mount.

The `wolveix/satisfactory-server` image's own documentation describes `/config/gamefiles` as the
downloaded server installation, kept outside the container only to avoid re-fetching 8GB+ on every
rebuild, and `/config/saved` as the game's blueprints, saves and server configuration. `/config/saved`
is not under `/config/gamefiles`, so it lives on the `satisfactory` claim, not `sf-gamedata`.

## History

Before PR #3879, the shared `components/volsync` template's default `sourcePVC: ${APP}` already
resolved to `satisfactory`, so backups covered the claim that appears to hold the actual saves. That
PR treated `satisfactory` as "the empty 5Gi PVC the component itself creates," reasoned that
`sf-gamedata` (30Gi) must be the real data because it is larger and explicitly provisioned in
`./pvc.yaml`, and repointed both `ReplicationSource` objects at it. Read against the mount paths and
the image's documentation, that reasoning has it backwards: `sf-gamedata` holds the replaceable
server install, and `satisfactory` holds `/config/saved`.

## Needed

Confirm what is actually on each PVC (for example, `just kube browse-pvc games satisfactory` and
`... sf-gamedata`, checking for a `saved/` directory), then repoint `sourcePVC` at whichever claim
holds it. If `satisfactory` does turn out to hold the saves, dropping the kustomization.yaml patch
entirely restores the component's own default. Until this is confirmed, do not assume either
`ReplicationSource`'s recent snapshots are restorable saves.

## Related

- PR #3879
