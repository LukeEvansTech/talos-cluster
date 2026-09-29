# KB-145: satisfactory's VolSync backup targeted the wrong PVC

**Status:** Resolved (2026-09-29). PR #3879 (2026-07-26) repointed both `ReplicationSource` objects
from `satisfactory` to `sf-gamedata`, believing `sf-gamedata` held the save data. It holds the
re-downloadable server install instead. The kustomization patch is now gone, so backups cover the
`satisfactory` claim again.

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

## Confirmation and fix

Checked on the live pod on 2026-09-29: `/config/saved` sits on the `satisfactory` claim, and
`/config/gamefiles` (about 2.9 GB) is the only thing on `sf-gamedata`. Both `ReplicationSource`
objects were backing up `sf-gamedata`. Removing the `kustomization.yaml` patch restores the
component's default `sourcePVC: ${APP}`. No PVC is created or pruned by the change: the component
already generated the `satisfactory` claim, and `sf-gamedata` stays in `./pvc.yaml`. Snapshots taken
before the fix contain only the server install, so they cannot restore saves.

## Related

- PR #3879
