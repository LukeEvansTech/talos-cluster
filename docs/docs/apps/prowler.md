# Prowler

## Purpose

Prowler App is a self-hosted security and compliance posture-management web UI. In
this cluster it runs continuous CIS / compliance scanning against the local
Kubernetes cluster, with a queryable findings dashboard.

- The upstream project is a Django (REST API) + Next.js (UI) + Celery stack and
  ships only a `docker-compose.yml` — no Helm chart.
- This deployment translates the compose stack into bjw-s `app-template`
  HelmReleases, reusing the cluster's existing building blocks:
  - CloudNativePG (`postgres18`) for the Django database
  - Dragonfly for the Celery broker and Django cache
  - A DozerDB (Neo4j-compatible) StatefulSet for the asset-graph feature
  - ExternalSecrets from 1Password and an `envoy-internal` HTTPRoute
- v1 scans only the in-cluster Kubernetes provider. Cloud providers
  (AWS / GCP / Azure / M365 / GitHub / Cloudflare) are wired up later from the UI.

## Design decisions

- **Three HelmReleases under `security/prowler/`** plus one for DozerDB under
  `database/dozerdb/`:
  - `prowler-api` — one Deployment with **two containers**, `api` (gunicorn) and
    `worker` (celery), sharing an `emptyDir` at `/tmp/prowler_api_output`, plus an
    `init-db` initContainer that bootstraps the database and role.
  - `prowler-ui` — the Next.js frontend (NextAuth lives here).
  - `prowler-beat` — the celery beat scheduler.
- **Co-locating API and worker in one Pod.** The worker writes scan artifacts that
  the API serves. The cluster has no RWX StorageClass (only RWO `ceph-block`,
  `miroir-local`, and `ceph-bucket` S3), so the two cannot share a PVC across
  separate Deployments. Running both as containers in one Pod with a shared
  `emptyDir` matches the compose volume-sharing semantics and avoids introducing an
  NFS provisioner.
- **Database bootstrap** uses the `postgres-init` initContainer pattern to create
  the `prowlerdb` database and `prowler` role on the existing CNPG cluster. Prowler's
  `POSTGRES_ADMIN_*` (used for partition management) is pointed at the same app role.
- **Path-routed HTTPRoute** on `envoy-internal` only, at `prowler.${SECRET_DOMAIN}`.
  Only `/api/v1` routes to the API backend;
  everything else (including NextAuth's `/api/auth/*` and `/api/health`) goes to the
  UI. Gateway API longest-prefix matching means `/api/v1` wins over `/`.
- **RBAC** is a ServiceAccount (`prowler`) bound to the built-in read-only `view`
  ClusterRole, used by the in-cluster Kubernetes provider. The SA is created by the
  app-template chart; a raw `ClusterRoleBinding` grants the cluster-wide read.
- **Deferred to a later pass:** narrowing `view` to a purpose-built role, OIDC SSO
  (Prowler natively supports only Google/GitHub OAuth), cloud-provider scanning, the
  Prowler MCP server, VolSync backup of the DozerDB PVC (graph data is rebuildable),
  and locking down self-signup after the first user.

## Deploy gotchas

- **The mode arg is baked into the image ENTRYPOINT, but both `command` and `args`
  must be overridden.** The same `prowler-api` image becomes API, worker, or beat
  depending on how the container is launched:
  - no overrides → the default entrypoint (`../docker-entrypoint.sh prod`) runs
    `migrate` + `pgpartition` + gunicorn
  - `command: ["../docker-entrypoint.sh"], args: ["beat"]` → celery beat
  - the worker container skips the entrypoint entirely: `command: ["/bin/sh", "-c"]` invoking
    `python -m celery -A config.celery worker` directly with an explicit queue list
    (celery, scans, scan-reports, deletion, backfill, overview, integrations, compliance,
    attack-paths-scans) and `--without-mingle --without-gossip`. Celery 5.6's mingle/gossip
    startup steps kill the worker instantly against Dragonfly's pub/sub emulation, and the
    entrypoint's own `worker` mode can't pass those flags through — direct invocation is the
    only way to disable them.
  The `command` override is required in the beat and worker cases. Without it, an appended
  `args` value is tacked onto the image's hardcoded `["../docker-entrypoint.sh", "prod"]`,
  which still runs gunicorn and causes a port 8080 collision with the API container.
- **`NEO4J_AUTH` cannot contain a `/`.** The value is `neo4j/<password>` and DozerDB
  parses it by splitting on the first `/`, so a generated password containing `/`
  silently corrupts the credential. Generate the DozerDB password without `/` (or
  re-roll until clean) before storing it in 1Password.
- **DozerDB needs `CAP_CHOWN`.** Its entrypoint chowns the data directory on startup,
  so dropping `ALL` capabilities breaks it. Do not strip capabilities on the DozerDB
  container the way the Prowler containers do.
- **Django `ALLOWED_HOSTS` rejects kubelet probe requests.** Kubelet probes hit the
  Pod by an address that is not in `DJANGO_ALLOWED_HOSTS`, and Django returns
  `400 Bad Request` (`DisallowedHost`), so the probe fails and the Pod never goes
  ready. `DJANGO_ALLOWED_HOSTS` is `prowler-api,prowler.${SECRET_DOMAIN}` (the
  service name plus the one route hostname) — it does not need to widen for the
  probe, because the liveness/readiness probes override the request's `Host` header
  to `prowler-api` instead. Expand the allowed-hosts list if Django still rejects a
  `Host` header (e.g. when traffic arrives under a different service DNS).
- **gunicorn auto-worker count blows memory.** Left to auto-detect, gunicorn spawns a
  worker per CPU core, which on a multi-core node far exceeds the container memory
  limit and OOMKills the API. Pin the worker count explicitly to a small value so
  memory stays within the request/limit.
- **Service `nameOverride` (resolved by renaming the HelmRelease).** The UI's
  `API_BASE_URL` and Django's `ALLOWED_HOSTS` require the API Service to resolve as
  `prowler-api`. app-template names the Service after the HelmRelease, so the
  HelmRelease was renamed to `prowler-api` (rather than relying on a `nameOverride`
  that not all chart versions honour) to ensure the Service name matches.
- **celery beat is a singleton.** Run exactly one `prowler-beat` replica with a
  `Recreate` strategy — two beat instances cause duplicate task scheduling.

## Operational notes

- **First-user signup is not locked down in v1.** Anyone with internal network access
  who reaches the UI before you do can claim the tenant. Register the first user
  immediately after the UI comes up; the first registered user becomes tenant owner.
- **DozerDB memory ceiling** is conservative for a homelab (1G heap). Watch for Pod
  restarts on the first large scan and bump the heap/limit if needed.
- **The `view` ClusterRole is broad** — it grants read on Secrets cluster-wide.
  Acceptable for v1; swap to a narrower role if that read surface is unwanted.
- **Adding the Kubernetes provider.** In the UI choose the in-cluster ServiceAccount
  option. If only kubeconfig upload is offered, mint a token for the `prowler`
  ServiceAccount and build a kubeconfig pointing at the in-cluster API endpoint
  (`https://kubernetes.default.svc.cluster.local`).
- **Health and verification.** A gatus `guarded` check covers the endpoint. After a
  reconcile, confirm the `init-db` initContainer created the database and role, the
  `api` container applied migrations and bound gunicorn on its port, the `worker`
  connected to the broker, and `prowler-beat` started its scheduler.
