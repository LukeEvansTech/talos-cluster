# Autopulse

Media-automation app in the `media` namespace (bjw-s app-template HelmRelease).
Watches sources and notifies media targets to rescan. Runs minimal: pod Ready,
web UI on the internal route, no triggers/targets wired yet.

## Purpose

- Bring autopulse online on image `v2.0.0` in a deliberately minimal config:
  - Pod becomes `Ready`; web UI reachable at `autopulse.${SECRET_DOMAIN}` and
      `autopulse.${SECRET_INTERNAL_DOMAIN}` (both served via `envoy-internal`, internal-only).
  - SQLite database persisted on a VolSync-backed PVC (NFS + remote backups).
  - Triggers/targets intentionally omitted — integrations are added later.
- Not in scope (for the minimal bring-up): wiring source/target integrations,
  or exposing the app externally.

## Design decisions

- **SQLite, not Postgres.** v2.0.0 changed the default `database_url` from
  PostgreSQL to a SQLite file (`sqlite://data/autopulse.db`, resolving to
  `/app/data/autopulse.db`). The minimal config embraces this — no database
  dependency, just a single file on a small PVC.
- **Single-file SQLite DB backed up with VolSync.** The whole "database" is one
  file living on a PVC, so the standard VolSync component backs it up exactly
  like any other app's data dir — no DB-specific dump/restore tooling needed:
  - The `volsync` component (added in `ks.yaml` `spec.components`, never also in
    `app/kustomization.yaml`) auto-creates the PVC `autopulse` (`ceph-block`),
    the NFS + remote `ReplicationSource`s, and the restore `ReplicationDestination`.
  - `VOLSYNC_CAPACITY: 1Gi` because a SQLite DB is tiny (the component default
    is 5Gi — override it down).
  - Backup credentials reuse the shared VolSync 1Password items; **no new backup
    secrets** are introduced.
- **Writable PVC at `/app/data`, root FS stays read-only.** The HelmRelease keeps
  `readOnlyRootFilesystem: true`; SQLite still works because the writable mount
  is the PVC at `/app/data`, not the root FS:

  ```yaml
  persistence:
    data:
      existingClaim: "{{ .Release.Name }}"
      globalMounts:
        - path: /app/data
  ```

- **Inline `config.yaml` in the ExternalSecret.** The earlier
  `templateFrom.configMap` pointed at a ConfigMap that never existed in git or
  the cluster, so it was dropped. The config is rendered inline; only the auth
  credentials come from 1Password, while the non-secret `database_url` lives in
  the template:

  ```yaml
  target:
    name: autopulse-secret
    template:
      engineVersion: v2
      data:
        config.yaml: |
          app:
            database_url: sqlite://data/autopulse.db
          auth:
            username: "{{ .username }}"
            password: "{{ .password }}"
  dataFrom:
    - extract:
        key: autopulse
  ```

## Deploy gotchas

- **An unconfigured v2 pod cannot start.** With `readOnlyRootFilesystem: true`
  and no writable mount at `/app/data`, v2 cannot create the SQLite file. The
  writable `data` PVC (above) is mandatory, not optional.
- **`existingClaim` must match the VolSync PVC name.** The HelmRelease uses
  `existingClaim: "{{ .Release.Name }}"`, which resolves to `autopulse` — the
  exact name the VolSync component derives from `APP`. PVC name, claim, and
  `ReplicationSource` `sourcePVC` must all agree.
- **Default credentials must be changed.** v2 ships `admin` / `password`
  defaults. Create the `autopulse` 1Password login item (username `admin` + a
  generated 32-char password) via `op item create` in the operator's signed-in
  shell — never the UI, never committed. The field labels `username`/`password`
  must match the `{{ .username }}` / `{{ .password }}` template vars.
- **Empty `triggers`/`targets` may be rejected.** The minimal config omits both
  maps (serde should default them to empty). If startup logs complain about
  missing keys, add explicit `triggers: {}` / `targets: {}` to the inline config.
- **First-deploy restore finds no snapshot.** Standard VolSync bootstrap — the
  `ReplicationDestination` provisions an empty PVC and the app initializes a
  fresh DB. No manual action.

## Operational notes

- **Enable/disable** lives in the namespace `kustomization.yaml`: the app entry
  `- ./autopulse/ks.yaml` is commented out when dormant. Keep it in alphabetical
  position when re-enabling.
- **Health endpoint** `/health` (port `2875`) is unauthenticated in v2 — handy
  for a quick liveness check from inside the pod without credentials.
- **Verify a healthy deploy** by checking, in order:
  - `Kustomization autopulse` reconciles `Ready=True`.
  - `ExternalSecret autopulse-secret` is `SecretSynced=True` and the rendered
    Secret carries a `config.yaml` with the auth block (a `SecretSyncedError`
    almost always means the 1Password item is in the wrong vault).
  - PVC `autopulse` is `Bound` (1Gi, `ceph-block`); `ReplicationSource`s for NFS
    and remote both exist.
  - Pod is `1/1 Ready`; logs show the SQLite DB created at
    `/app/data/autopulse.db` with no auth/config-load errors.
