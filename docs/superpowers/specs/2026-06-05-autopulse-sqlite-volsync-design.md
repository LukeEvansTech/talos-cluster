# Enable autopulse v2 (minimal) — SQLite on VolSync

Date: 2026-06-05
Status: Approved (design)
App: `kubernetes/apps/media/autopulse`

## Goal

Bring the dormant autopulse app online on image `v2.0.0`, in a **minimal** configuration:

- Pod becomes `Ready` and the web UI is reachable at the internal route.
- Database is SQLite, persisted on a VolSync-backed PVC (NFS + R2 backups).
- No triggers/targets configured yet (integrations added later).

Non-goals: wiring Sonarr/Radarr/Plex/etc. triggers and targets; exposing externally.

## Background

- autopulse is currently **not deployed**: it is commented out in
  `kubernetes/apps/media/kustomization.yaml` behind
  `# TODO: Update externalsecret with autopulse config before enabling`.
  No pod / HelmRelease / ExternalSecret / Flux Kustomization exists in the cluster.
- The image tag was bumped to `v2.0.0` by Renovate PR #2849 (merged).
- **v2.0.0 breaking change:** the default `database_url` switched from PostgreSQL
  to SQLite (`sqlite://data/autopulse.db`, resolving to `/app/data/autopulse.db`).
  The HelmRelease runs with `readOnlyRootFilesystem: true` and has no writable
  volume at `/app/data`, so an unconfigured v2 pod cannot create the SQLite file.
- The existing `externalsecret.yaml` referenced a `templateFrom.configMap:
  autopulse-config` that does not exist in git or the cluster. The only other app
  using that indirection (`garage`) also references a missing ConfigMap, so the
  pattern was never completed here. We replace it with an inline template.

## v2 config facts (verified)

- `app.database_url`: SQLite file form is `sqlite://data/autopulse.db`
  (in-memory: `sqlite://:memory:`; Postgres: `postgres://...`).
- `auth.username` / `auth.password`: default `admin` / `password` — must be changed;
  not committed to git.
- `triggers` / `targets`: maps; omitted entirely for the minimal config (serde
  defaults to empty). Fallback: explicit `triggers: {}` / `targets: {}` if the
  loader rejects their absence.
- Sources: https://github.com/dan-online/autopulse ,
  https://github.com/dan-online/autopulse/blob/main/example/docker-compose.yml

## Design

All changes on branch `feat/autopulse-sqlite-volsync` (not `main`).

### 1. ExternalSecret — inline the config (`app/externalsecret.yaml`)

Drop `templateFrom.configMap`; render `config.yaml` inline. Only the auth
credentials come from 1Password; the SQLite URL is non-secret and lives in the
template.

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

### 2. HelmRelease — writable data volume (`app/helmrelease.yaml`)

Add a VolSync-backed `data` persistence mounted at `/app/data`. Keep
`readOnlyRootFilesystem: true` (the PVC is writable). Leave the existing config
secret mount, `/tmp` emptyDir, and read-only media NFS mount unchanged.

```yaml
persistence:
  data:
    existingClaim: "{{ .Release.Name }}"
    globalMounts:
      - path: /app/data
```

### 3. Flux Kustomization — add VolSync (`ks.yaml`)

Add the VolSync component and substitutions alongside the existing ones.

```yaml
components:
  - ../../../../components/gatus/guarded
  - ../../../../components/homepage
  - ../../../../components/volsync
postBuild:
  substitute:
    APP: autopulse
    VOLSYNC_CAPACITY: 1Gi   # SQLite db is tiny
    # existing HOMEPAGE_* substitutions retained
```

The VolSync component auto-creates: PVC `autopulse` (storageClass `ceph-block`),
NFS (kopia) + R2 (restic) `ReplicationSource`s, and the restore
`ReplicationDestination`. Backup credentials come from the shared
`volsync-template` / `volsync-r2-template` 1Password items — **no new backup
secrets**.

### 4. Enable in namespace kustomization (`media/kustomization.yaml`)

Un-comment `- ./autopulse/ks.yaml` and remove the stale TODO comment line above it.

### 5. Cleanup

Delete the now-unreferenced empty `app/resources/` directory.

## Secret to create (manual, one-off)

A `autopulse` login item in the cluster-secrets vault the 1Password Connect server
reads, with fields `username` (`admin`) and a generated 32-char `password`.
Created via `op item create` in the operator's signed-in shell. The exact command
is produced at implementation time, before the manifests reconcile.

## Verification

A change is correct when all hold:

1. Flux `Kustomization autopulse` reconciles `Ready=True`.
2. `ExternalSecret autopulse-secret` is `SecretSynced=True`; rendered
   `autopulse-secret` contains a `config.yaml` with the auth block.
3. PVC `autopulse` is `Bound`.
4. Pod is `Ready` (1/1); logs show the SQLite DB created at `/app/data/autopulse.db`
   with no auth/config load errors.
5. Web UI reachable at `autopulse.<SECRET_INTERNAL_DOMAIN>`.
6. `ReplicationSource autopulse-nfs` and `autopulse-r2` exist.

## Risks & mitigations

- **Empty triggers/targets rejected by the v2 loader** — omit the keys (serde
  default); if startup logs complain, add explicit `triggers: {}` / `targets: {}`.
- **First-deploy VolSync restore finds no snapshot** — standard onedr0p bootstrap;
  the `ReplicationDestination` provisions an empty PVC and the app initializes
  fresh. Same path taken by every other VolSync app here.
- **1Password item created in the wrong vault** — ExternalSecret stays
  `SecretSyncedError`; recreate the item in the correct vault.
