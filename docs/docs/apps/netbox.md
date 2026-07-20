# NetBox

## Purpose

[NetBox](https://github.com/netbox-community/netbox) provides DCIM (data centre infrastructure
management) and IPAM (IP address management) — the source of truth for devices, racks, prefixes, and
IP allocations. It runs internal-only in the `default` namespace: a web frontend, an RQ background
worker (webhooks, scripts, reports), and a daily housekeeping CronJob.

## Design decisions

NetBox is one of the few apps here that does **not** use the bjw-s `app-template` chart. It uses the
official **`netbox-chart`** (`oci://ghcr.io/netbox-community/netbox-chart/netbox`) via an
`OCIRepository` + `chartRef`, because NetBox's multi-workload topology (web + worker + housekeeping)
and its `existingSecret` contract are already modelled by the upstream chart.

- **Namespace** `default`, modelled on the in-repo `paperless` app (its closest twin).
- **PostgreSQL** — shared CNPG cluster (`postgres18-rw.database.svc.cluster.local`, `sslmode=require`);
  bundled Postgres disabled. An `init-db` initContainer (using the `postgres-init` image) creates
  the `netbox` role + database using the CNPG superuser before first boot.
- **Redis** — external **Dragonfly** (`dragonfly.database.svc.cluster.local:6379`); the chart's
  bundled cache (`valkey.enabled`) is disabled. Separate logical DB indices for the tasks queue
  (`0`) vs the cache (`1`).
- **Media** — a local `ceph-block` PVC consumed via `persistence.existingClaim` and backed up by the
  VolSync component (`VOLSYNC_CAPACITY`). NetBox uses no object storage; the cluster's S3 tier is
  Garage (`storage` namespace), not Ceph RGW (which was removed).
- **Auth / exposure** — superuser-only first pass (pocket-id OIDC is a fast-follow); internal-only
  HTTPRoute on `envoy-internal`, never publicly exposed.
- Health monitored automatically by the gatus-sidecar (it auto-discovers the HTTPRoute);
  `reloader.stakater.com/auto` rolls pods on secret/config change; a ServiceMonitor exposes metrics
  to kube-prometheus-stack.

## Deploy gotchas

- **One shared `netbox-secret` must carry the chart's EXACT key names.** All of the chart's secret
  references (global `existingSecret`, `superuser.existingSecret`, `externalDatabase` /
  `tasksDatabase` / `cachingDatabase` `existingSecretName`) point at the single `netbox-secret`, so
  that Secret must contain every key the chart projects, spelled exactly as the chart expects:
  - `secret_key`, `email_password` (the chart's projected `secrets` volume **requires the key to
    exist** even when email is unused — set it to an empty string).
  - `password` and `api_token` (superuser).
  - `db_password` (the value of `externalDatabase.existingSecretKey`).
  - `tasks_password` and `cache_password` (the Dragonfly tasks/caching DB keys).
- These mismatches do **not** show up in a `flate` (local) or Konflate (in-cluster PR render) —
    the manifest is valid. They only fail at runtime as pod `FailedMount` (`references
    non-existent secret key`) or `CreateContainerConfigError` (`couldn't find key …`). Before
    wiring the ExternalSecret, render the chart and grep the output for every `secretKeyRef`
    `key:` and projected-volume `items[].key`:

    ```bash
    flate build hr netbox -n default --path kubernetes/flux/cluster
    ```

- **Web, worker, and housekeeping share one RWO `media` PVC, so they must co-locate.** With a
  ReadWriteOnce `ceph-block` claim, scheduling the pods onto different nodes deadlocks on
  `Multi-Attach` during a rollout. Pin them together with podAffinity (anchor on the worker) or hit
  a stuck rollout.
- **Granian web OOMs at the chart's default 4 workers** (peaks ~956Mi against a 1Gi limit). Set
  `GRANIAN_WORKERS=2` and give the web pod request 512Mi / limit 1.5Gi.
- **Django host guarding rejects unlisted Host headers, including kubelet probes.** `ALLOWED_HOSTS`
  must include both the route host(s) and the in-cluster service name (the chart's
  `allowedHostsIncludesPodIP` covers the pod IP), and CSRF trusted origins must list both
  `https://netbox.${SECRET_DOMAIN}` and `https://netbox.${SECRET_INTERNAL_DOMAIN}`. Miss this and
  probes (and the UI) get a 400.
- **`SECRET_KEY` must be 50+ characters.** Generate it long enough (e.g. `openssl rand -base64 60`)
  or NetBox refuses to start.
- The 1Password source item lives in the `Talos` vault and is created via the `op` CLI (never the
  UI), with values generated locally.

## Operational notes

- **Reconcile** lives in the `default` namespace; it `dependsOn` `cloudnative-pg-cluster` and
  `dragonfly-cluster` in the `database` namespace, so reconcile those first if NetBox shows
  "dependency not ready".

  ```bash
  flux reconcile kustomization netbox -n default
  ```

- **Hostname** is `netbox.${SECRET_INTERNAL_DOMAIN}` (internal DNS via external-dns). Listing the
  `${SECRET_DOMAIN}` host on the `envoy-internal` listener keeps it internal-only — it does not
  expose NetBox publicly.
- **First-boot check** — confirm the `init-db` initContainer reports the `netbox` role/database
  created (or already exists), then log in as the superuser (`admin`).
- **Backups** — the `media` PVC is snapshotted by VolSync. The `netbox` database lives on the shared
  CNPG cluster and is backed up by CNPG's own path, not VolSync.
- **Teardown** — `prune: true` removes NetBox's Kubernetes resources on revert, but the `netbox`
  database on the shared CNPG cluster and the 1Password item persist and must be dropped manually
  for a clean teardown.
