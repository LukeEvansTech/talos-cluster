# NetBox — Design

**Date:** 2026-05-29
**Status:** Approved
**Branch:** `feat/netbox`

## Goal

Stand up [NetBox](https://github.com/netbox-community/netbox) (DCIM/IPAM) on the
cluster following this repo's standard app patterns — modelled on the existing
`paperless` app, which is the closest in-repo twin (shared CNPG Postgres +
`postgres-init`, Dragonfly redis, VolSync media backup, pocket-id OIDC available).

Reference implementations reviewed: `jfroy/flatops` (best structural match —
CNPG + `postgres-init` + Rook RGW + envoy-internal + VolSync), `drag0n141/home-ops`
(external Dragonfly + pocket-id OIDC), `Mafyuh/iac` (minimal 4-file layout).

## Decisions

| Decision              | Choice                               | Rationale                                                                                      |
| --------------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Namespace             | `default`                            | Matches references and in-repo twins (`paperless`, `tandoor`, `whodb`).                        |
| Redis                 | External **Dragonfly**               | Repository standard; `paperless` already uses it. Bundled Valkey disabled.                     |
| Media storage         | Local **`ceph-block` PVC** + VolSync | Fewest moving parts; proven `paperless` pattern. (Ceph RGW S3 is available but not used here.) |
| Worker + housekeeping | **Both enabled**                     | "Real" NetBox: RQ worker for webhooks/scripts/reports + housekeeping CronJob.                  |
| Auth                  | **Superuser only (first pass)**      | Get core working; pocket-id OIDC is a fast follow.                                             |
| Exposure              | **Internal only**                    | DCIM/IPAM is not public. HTTPRoute on `envoy-internal`.                                        |

## File Layout

Standard 4-file app under `kubernetes/apps/default/netbox/`:

```text
kubernetes/apps/default/netbox/
├── ks.yaml                      # Flux Kustomization (&app netbox)
└── app/
    ├── helmrelease.yaml         # OCIRepository + HelmRelease
    ├── externalsecret.yaml      # 1Password → netbox-secret
    ├── httproute.yaml           # envoy-internal route
    └── kustomization.yaml
```

The namespace `kustomization.yaml` at `kubernetes/apps/default/kustomization.yaml`
gets a new entry for `./netbox/ks.yaml` (alphabetical order).

## Components

`ks.yaml` `spec.components` (NOT also in `app/kustomization.yaml` — Flux applies
ks.yaml components on top of the path build):

- `../../../../components/gatus/guarded` — health monitoring
- `../../../../components/volsync` — media PVC backup (`VOLSYNC_CAPACITY`)

`postBuild.substitute`: `APP: *app`, `VOLSYNC_CAPACITY: 5Gi`.

`dependsOn`:

- `cloudnative-pg-cluster` (namespace `database`)
- `dragonfly-cluster` (namespace `database`)
- `onepassword` is reached via the global chain; the ExternalSecret store
  `onepassword-connect` is used directly.

## Chart Source

`OCIRepository` → `oci://ghcr.io/netbox-community/netbox-chart/netbox`, pinned to
the current 8.2.x tag (verified at implementation against the published chart;
Renovate manages thereafter). `chartRef.kind: OCIRepository` in the HelmRelease.

## Data Plane

### PostgreSQL (shared CNPG `postgres18`)

- `postgresql.enabled: false`
- `externalDatabase`: host `postgres18-rw.database.svc.cluster.local`, port `5432`,
  database `netbox`, username `netbox`, password from `netbox-secret`, `sslmode=require`.
- A `postgres-init` initContainer (`ghcr.io/home-operations/postgres-init`) creates
  the `netbox` role + database using the CNPG superuser, mirroring `paperless`:
    - `INIT_POSTGRES_HOST`, `INIT_POSTGRES_DBNAME=netbox`, `INIT_POSTGRES_USER=netbox`,
      `INIT_POSTGRES_PASS` (app password)
    - `INIT_POSTGRES_SUPER_USER` / `INIT_POSTGRES_SUPER_PASS` from 1Password `cloudnative-pg`.

### Redis (Dragonfly)

- Bundled `valkey.enabled: false`.
- `externalRedis` → `dragonfly.database.svc.cluster.local:6379`, auth user `default`,
  password from 1Password `dragonfly` (`DRAGONFLY_PASSWORD`).
- Separate logical DB indices for the tasks queue vs the cache (e.g. tasks `0`,
  cache `1`) — final index values set per the chart's `externalRedis` schema.

### Media

- `persistence.enabled: true`, `ceph-block` StorageClass PVC, mounted at NetBox's
  media path. Backed up by the VolSync component.

## Workloads

- **web** (default), **worker** (`worker.enabled: true` — RQ background jobs),
  **housekeeping** (`housekeeping.enabled: true` — daily CronJob).
- `metrics.serviceMonitor.enabled: true` (scraped by kube-prometheus-stack).
- `commonAnnotations: reloader.stakater.com/auto: "true"`.
- `resourcesPreset` / explicit requests+limits set to sane values at implementation.

## Networking

- `HTTPRoute` `netbox` on `parentRefs: envoy-internal` (namespace `network`).
- Hostname `netbox.${SECRET_INTERNAL_DOMAIN}` (internal DNS via external-dns → OPNsense).
- Backend: service `netbox`, port `80`.
- **Django host guarding:** set `ALLOWED_HOSTS` to include the route host and the
  in-cluster service name, and set CSRF trusted origins to
  `https://netbox.${SECRET_INTERNAL_DOMAIN}`. NetBox/Django rejects requests with an
  unlisted Host header, including kubelet probes — see prior paperless/Prowler
  ALLOWED_HOSTS lessons.

## Secrets

Single `ExternalSecret` (`onepassword-connect` ClusterSecretStore,
`engineVersion: v2`) producing `netbox-secret`, with `dataFrom` extracting:

- `cloudnative-pg` — `POSTGRES_SUPER_USER`, `POSTGRES_SUPER_PASS`
- `dragonfly` — `DRAGONFLY_PASSWORD`
- `netbox` (NEW item) — superuser password, API token, `SECRET_KEY`, app DB password

The `netbox` 1Password item is created via the `op` CLI into the `Talos` vault
(never the UI), with values generated locally:

```bash
op item create --vault Talos --category login --title netbox \
  NETBOX_SUPERUSER_PASSWORD="$(openssl rand -base64 24)" \
  NETBOX_SUPERUSER_API_TOKEN="$(openssl rand -hex 20)" \
  NETBOX_SECRET_KEY="$(openssl rand -base64 60 | tr -d '\n')" \
  NETBOX_POSTGRES_PASSWORD="$(openssl rand -base64 24)"
```

(Exact field names reconciled with the chart's expected `existingSecret` keys —
see "To verify" below.)

## To Verify At Implementation (chart-schema specifics, not assumptions)

1. Pull `netbox-chart` 8.2.x `values.yaml` and confirm:
    - The exact `existingSecret` **key names** the chart expects
      (e.g. `db_password`, `redis_password`, `redis_tasks_password`,
      `superuser_password`, `superuser_api_token`, `secret_key`).
    - The `externalRedis` value schema (tasks vs caching host/port/db/secret keys)
      and the correct flag to disable the bundled cache (`valkey.enabled` vs
      `redis.enabled` for this chart major).
    - The media mount path and the persistence value keys.
2. Confirm the current published chart tag.
3. Map the `netbox` 1Password field names to the chart's `existingSecret` keys.

## Out of Scope (fast follow)

- pocket-id OIDC remote auth + auto-create (mirror `paperless`
  `PAPERLESS_SOCIALACCOUNT_PROVIDERS` style).
- Public exposure / external hostname.
- Ceph RGW S3 media backend.
- NetBox plugins.

## Verification

- `flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster`
  passes locally and in the `flux-local` CI workflow.
- `just lint` (super-linter) clean.
- After deploy: HelmRelease `Ready`, web + worker + housekeeping pods healthy,
  `postgres-init` completes, NetBox UI reachable at the internal host, login as
  superuser, Gatus health check green.
