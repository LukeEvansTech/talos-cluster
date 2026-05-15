# Prowler App Self-Hosted Design

## Overview

Deploy [Prowler](https://github.com/prowler-cloud/prowler) App — a self-hosted security/compliance posture management web UI — into the cluster. v1 scans the local Kubernetes cluster only; other providers (AWS, GCP, Azure, M365, GitHub, Cloudflare) are wired up later from the UI.

Prowler App is a Django + Next.js stack. The upstream project ships no Helm chart; only `docker-compose.yml`. This design translates that compose stack into bjw-s `app-template` HelmReleases following the repo's existing patterns (CNPG for Postgres, Dragonfly for Redis/Valkey, ExternalSecrets from 1Password, envoy-internal HTTPRoute, gatus health check).

Purpose: continuous CIS / compliance scanning of the cluster with a queryable findings UI.

## Scope

**In scope (v1):**

- 3 Prowler HelmReleases in `security/prowler/`:
  - `prowler-app` — single Deployment with **two containers** (`api` running gunicorn, `worker` running celery) sharing an emptyDir at `/tmp/prowler_api_output`
  - `prowler-ui`
  - `prowler-beat`
- DozerDB (Neo4j-compatible) StatefulSet in `database/dozerdb/` for the asset-graph feature
- New Postgres database `prowlerdb` + role `prowler` on the existing `postgres18` CNPG cluster
- Reuses existing Dragonfly for Celery broker / cache
- Internal-only ingress on `envoy-internal` at `prowler.${SECRET_DOMAIN}` and `prowler.${SECRET_INTERNAL_DOMAIN}`
- ServiceAccount + ClusterRoleBinding (built-in `view`) for the in-cluster Kubernetes provider
- Gatus `guarded` health check via the existing component

**Why co-locate api + worker?** The api serves scan artifacts that the worker writes to `/tmp/prowler_api_output`. The cluster has no RWX StorageClass (only RWO `ceph-block`, `openebs-hostpath`, and `ceph-bucket` S3) so the two services cannot share a PVC across Deployments. Running both as containers in one Pod with a shared `emptyDir` matches the docker-compose semantics (volume sharing on a single host) and avoids introducing an NFS provisioner.

**Out of scope (deferred to v2):**

- Cloud-provider scanning (AWS/GCP/Azure/M365/GitHub/Cloudflare) — configured later in the UI with per-provider ExternalSecrets when needed
- OIDC SSO via pocket-id — Prowler natively supports only Google/GitHub OAuth, not generic OIDC
- `prowlercloud/prowler-mcp` MCP server
- VolSync backup of DozerDB PVC (graph data is rebuildable from scans)
- CiliumNetworkPolicy (matches the absence on pocket-id; cluster-wide policies still apply)
- Narrower ClusterRole (replace built-in `view` with upstream's `prowler-role` later if `view` proves too broad)
- Self-signup lockdown after first user (set the appropriate Django env var once verified)

## Architecture

```text
┌─ namespace: security ──────────────────────────────────────────────────┐
│                                                                        │
│   prowler-app   Deployment, 1 replica                                  │
│   ┌─ Pod ──────────────────────────────────────────────────────────┐  │
│   │  initContainer: init-db (postgres-init) creates DB              │  │
│   │  container: api    gunicorn :8080  (entrypoint: prod)           │  │
│   │  container: worker celery worker, all queues                    │  │
│   │  shared emptyDir at /tmp/prowler_api_output                     │  │
│   │  ServiceAccount: prowler (cluster "view")                       │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│   prowler-ui    Deployment    next.js  :3000                           │
│                                                                        │
│   prowler-beat  Deployment    celery beat (1 replica, singleton)       │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
        │                  │                   │
        ▼                  ▼                   ▼
┌─ namespace: database ───────────────────────────────────────────────┐
│   postgres18-rw  CNPG       db: prowlerdb, role: prowler           │
│   dragonfly      existing   Celery broker + Django cache            │
│   dozerdb        NEW StatefulSet  graphstack/dozerdb:5.26.3.0       │
│                  PVC 10Gi ceph-block at /data                       │
│                  Service bolt :7687                                 │
└─────────────────────────────────────────────────────────────────────┘

ingress: envoy-internal
  HTTPRoute prowler.${SECRET_DOMAIN} + prowler.${SECRET_INTERNAL_DOMAIN}
    /api/*   → prowler-api:8080
    /*       → prowler-ui:3000
```

## Repo layout

```text
kubernetes/apps/database/dozerdb/
├── ks.yaml
└── app/
    ├── helmrelease.yaml          # bjw-s app-template, statefulset controller, 1 replica
    ├── externalsecret.yaml       # NEO4J_AUTH from 1Password "dozerdb"
    ├── ocirepository.yaml        # app-template OCIRepository
    └── kustomization.yaml

kubernetes/apps/security/prowler/
├── ks.yaml                       # components: gatus/guarded; dependsOn cnpg + dozerdb
└── app/
    ├── kustomization.yaml
    ├── ocirepository.yaml        # app-template OCIRepository (shared across HRs)
    ├── externalsecret.yaml       # → Secret consumed by app (api+worker) and beat
    ├── externalsecret-ui.yaml    # → Secret consumed by ui (AUTH_SECRET only)
    ├── helmrelease-app.yaml      # api + worker containers, init-db initContainer
    ├── helmrelease-ui.yaml
    ├── helmrelease-beat.yaml
    ├── httproute.yaml            # path-routed
    └── rbac.yaml                 # ServiceAccount + ClusterRoleBinding to "view"
```

Registrations:

- `kubernetes/apps/database/kustomization.yaml` → add `./dozerdb/ks.yaml`
- `kubernetes/apps/security/kustomization.yaml` → add `./prowler/ks.yaml` (alphabetical between anubis and pocket-id)

## Component detail

### dozerdb (database namespace)

- **Image:** `graphstack/dozerdb:5.26.3.0` (Renovate-pinned with digest)
- **Controller:** statefulset, 1 replica
- **Service:** `dozerdb.database.svc.cluster.local:7687` (bolt)
- **PVC:** 10Gi on `ceph-block`, mounted `/data`
- **Env (config):**
  - `NEO4J_dbms_max__databases=1000`
  - `NEO4J_server_memory_pagecache_size=512M`
  - `NEO4J_server_memory_heap_initial__size=512M`
  - `NEO4J_server_memory_heap_max__size=1G`
  - `NEO4J_PLUGINS=["apoc"]`
  - `NEO4J_dbms_security_procedures_allowlist=apoc.*`
  - `NEO4J_apoc_export_file_enabled=false`
  - `NEO4J_apoc_import_file_enabled=false`
  - `NEO4J_apoc_trigger_enabled=false`
  - `NEO4J_dbms_connector_bolt_listen_address=0.0.0.0:7687`
- **Env (from secret):** `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}`
- **Probes:** TCP on 7687 (readiness + liveness)
- **Resources:** request 100m / 1.5Gi, limit 2Gi memory

### prowler-app (security namespace) — api + worker

- **Image (both containers):** `prowlercloud/prowler-api:stable` pinned to digest (Renovate handles)
- **Controller:** deployment, 1 replica
- **Service:** `prowler-api.security.svc.cluster.local:8080` (exposes only the `api` container port)
- **ServiceAccount:** `prowler` (binds to ClusterRole `view`)
- **initContainer `init-db`:** `ghcr.io/home-operations/postgres-init:18@...` — envFrom shared secret, creates `prowlerdb` + `prowler` role with the `INIT_POSTGRES_*` vars

**Container `api`:**

- **Entrypoint:** default — `/home/prowler/docker-entrypoint.sh prod` runs `migrate` + `pgpartition` + gunicorn
- **Probes:** HTTP GET `/api/v1/` on 8080
- **Resources:** request 100m / 512Mi, limit 1Gi memory

**Container `worker`:**

- **args:** `["worker"]` → entrypoint runs celery worker against all queues
- No probes (celery worker)
- **Resources:** request 100m / 512Mi, limit 2Gi memory (scans can be spiky)

**Shared by both containers:**

- **Env (config):**
  - `DJANGO_SETTINGS_MODULE=config.django.production`
  - `DJANGO_BIND_ADDRESS=0.0.0.0`
  - `DJANGO_PORT=8080`
  - `DJANGO_ALLOWED_HOSTS=prowler-api,prowler.${SECRET_DOMAIN},prowler.${SECRET_INTERNAL_DOMAIN}`
  - `DJANGO_LOGGING_FORMATTER=ndjson`
  - `DJANGO_MANAGE_DB_PARTITIONS=True`
  - `TZ=${TIMEZONE}`
- **Env (from secret):** all `POSTGRES_*`, `INIT_POSTGRES_*`, `VALKEY_*`, `NEO4J_*`, `DJANGO_TOKEN_SIGNING_KEY`, `DJANGO_TOKEN_VERIFYING_KEY`, `DJANGO_SECRETS_ENCRYPTION_KEY`
- **Mounts (both containers):**
  - emptyDir `output` at `/tmp/prowler_api_output` (the shared volume worker writes / api serves)
  - emptyDir at `/home/prowler/.config/prowler-api`
  - emptyDir at `/tmp`

### prowler-ui (security namespace)

- **Image:** `prowlercloud/prowler-ui:stable` pinned to digest
- **Controller:** deployment, 1 replica
- **Service:** `prowler-ui.security.svc.cluster.local:3000`
- **Env (config):**
  - `API_BASE_URL=http://prowler-api:8080/api/v1` (server-side fetch from UI pod)
  - `NEXT_PUBLIC_API_BASE_URL=https://prowler.${SECRET_DOMAIN}/api/v1` (browser-side)
  - `NEXT_PUBLIC_API_DOCS_URL=https://prowler.${SECRET_DOMAIN}/api/v1/docs`
  - `AUTH_URL=https://prowler.${SECRET_DOMAIN}`
  - `AUTH_TRUST_HOST=true`
  - `UI_PORT=3000`
  - `TZ=${TIMEZONE}`
- **Env (from secret):** `AUTH_SECRET`
- **Probes:** HTTP GET `/api/health` on 3000
- **Resources:** request 50m / 256Mi, limit 512Mi memory

### prowler-beat (security namespace)

- Same image, **args:** `["beat"]`
- envFrom the shared secret
- **1 replica only** — celery beat is a singleton; multiple instances cause duplicate scheduling
- **Resources:** request 10m / 64Mi, limit 128Mi memory
- No probes

## Networking

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: prowler
spec:
  parentRefs:
    - name: envoy-internal
      namespace: network
  hostnames:
    - "prowler.${SECRET_DOMAIN}"
    - "prowler.${SECRET_INTERNAL_DOMAIN}"
  rules:
    # Prowler REST API — most specific match first
    - matches:
        - path:
            type: PathPrefix
            value: /api/v1
      backendRefs:
        - name: prowler-api
          port: 8080
    # Everything else, including NextAuth's /api/auth/* and /api/health, goes to the UI.
    # Gateway API matches longest-prefix first, so /api/v1 above wins over this /.
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: prowler-ui
          port: 3000
```

Both domains are internal-only (envoy-internal), matching the pocket-id pattern. external-dns publishes them to OPNsense.

**Why this split:** NextAuth's callback handler lives at `/api/auth/*` *inside the UI container*, not the Prowler REST API. Prowler's REST API is under `/api/v1/*`. Routing only `/api/v1` to the api Deployment keeps NextAuth on the UI where it belongs.

## RBAC

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: prowler
  namespace: security
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prowler-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view            # built-in, read-only across most resources cluster-wide
subjects:
  - kind: ServiceAccount
    name: prowler
    namespace: security
```

The `prowler-app` Deployment (both `api` and `worker` containers) uses this ServiceAccount. The worker container actually runs the cluster scans; the api container needs read access for provider auto-discovery flows in the UI.

`view` is broad but covers exactly the read surface CIS-Kubernetes checks need. A narrower replacement (modeled on upstream's `prowler-role.yaml`) is deferred to v2.

## Secrets

### Generated locally and stored in 1Password (one-shot setup)

Run before pushing manifests; values never leave the shell:

```bash
SIGNING_KEY="$(openssl genrsa 2048 2>/dev/null)"
VERIFYING_KEY="$(printf '%s' "$SIGNING_KEY" | openssl rsa -pubout 2>/dev/null)"
ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
AUTH_SECRET="$(openssl rand -base64 32)"
DBPASS="$(openssl rand -base64 24)"
NEO4J_PASSWORD="$(openssl rand -base64 24)"

op item create --vault Talos --category=Login --title=prowler \
  "PROWLER_DBUSER[text]=prowler" \
  "PROWLER_DBPASS[concealed]=$DBPASS" \
  "DJANGO_TOKEN_SIGNING_KEY[concealed]=$SIGNING_KEY" \
  "DJANGO_TOKEN_VERIFYING_KEY[concealed]=$VERIFYING_KEY" \
  "DJANGO_SECRETS_ENCRYPTION_KEY[concealed]=$ENCRYPTION_KEY" \
  "AUTH_SECRET[concealed]=$AUTH_SECRET"

op item create --vault Talos --category=Login --title=dozerdb \
  "NEO4J_PASSWORD[concealed]=$NEO4J_PASSWORD"
```

### 1Password items (Talos vault)

| Item             | Fields                                                                                                                                   | Status   |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| `prowler`        | `PROWLER_DBUSER`, `PROWLER_DBPASS`, `DJANGO_TOKEN_SIGNING_KEY`, `DJANGO_TOKEN_VERIFYING_KEY`, `DJANGO_SECRETS_ENCRYPTION_KEY`, `AUTH_SECRET` | **new**  |
| `dozerdb`        | `NEO4J_PASSWORD`                                                                                                                          | **new**  |
| `cloudnative-pg` | `POSTGRES_SUPER_USER`, `POSTGRES_SUPER_PASS`                                                                                              | existing |

### ExternalSecret → K8s Secret

**`prowler-secret`** (security ns, consumed by prowler-app api+worker containers and prowler-beat):

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: prowler
spec:
  secretStoreRef: {kind: ClusterSecretStore, name: onepassword-connect}
  target:
    name: prowler-secret
    template:
      engineVersion: v2
      data:
        # Postgres - app connection
        POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
        POSTGRES_PORT: "5432"
        POSTGRES_DB: prowlerdb
        POSTGRES_USER: "{{ .PROWLER_DBUSER }}"
        POSTGRES_PASSWORD: "{{ .PROWLER_DBPASS }}"
        POSTGRES_ADMIN_USER: "{{ .PROWLER_DBUSER }}"
        POSTGRES_ADMIN_PASSWORD: "{{ .PROWLER_DBPASS }}"
        # init-db (postgres-init pattern)
        INIT_POSTGRES_DBNAME: prowlerdb
        INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
        INIT_POSTGRES_USER: "{{ .PROWLER_DBUSER }}"
        INIT_POSTGRES_PASS: "{{ .PROWLER_DBPASS }}"
        INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
        INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
        # Valkey / Dragonfly
        VALKEY_HOST: dragonfly.database.svc.cluster.local
        VALKEY_PORT: "6379"
        VALKEY_DB: "0"
        # Neo4j / DozerDB
        NEO4J_HOST: dozerdb.database.svc.cluster.local
        NEO4J_PORT: "7687"
        NEO4J_USER: neo4j
        NEO4J_PASSWORD: "{{ .NEO4J_PASSWORD }}"
        # Django crypto
        DJANGO_TOKEN_SIGNING_KEY: "{{ .DJANGO_TOKEN_SIGNING_KEY }}"
        DJANGO_TOKEN_VERIFYING_KEY: "{{ .DJANGO_TOKEN_VERIFYING_KEY }}"
        DJANGO_SECRETS_ENCRYPTION_KEY: "{{ .DJANGO_SECRETS_ENCRYPTION_KEY }}"
  dataFrom:
    - extract: {key: cloudnative-pg}
    - extract: {key: prowler}
    - extract: {key: dozerdb}
```

**`prowler-ui-secret`** (security ns): `AUTH_SECRET` from `prowler`.

**`dozerdb-secret`** (database ns): `NEO4J_AUTH=neo4j/{{ .NEO4J_PASSWORD }}` from `dozerdb`.

### cluster-secrets

No new keys needed. `${SECRET_DOMAIN}`, `${SECRET_INTERNAL_DOMAIN}`, `${TIMEZONE}` already provided.

## Flux Kustomization (`ks.yaml`)

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app prowler
  namespace: &namespace security
spec:
  components:
    - ../../../../components/gatus/guarded
  dependsOn:
    - name: cloudnative-pg-cluster
      namespace: database
    - name: dragonfly
      namespace: database
    - name: dozerdb
      namespace: database
  interval: 1h
  path: ./kubernetes/apps/security/prowler/app
  postBuild:
    substitute:
      APP: *app
      GATUS_SUBDOMAIN: prowler
  prune: true
  retryInterval: 2m
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system
  targetNamespace: *namespace
  timeout: 5m
  wait: false
```

DozerDB's own `ks.yaml` mirrors this pattern but lives under `kubernetes/apps/database/dozerdb/ks.yaml` and has no Flux dependsOn (it bootstraps independently).

## Verification

After Flux reconciles:

1. `flux get hr -n security` shows all three prowler HRs ready (prowler-app, prowler-ui, prowler-beat)
2. `flux get hr -n database` shows `dozerdb` ready
3. `kubectl get po -n security -l app.kubernetes.io/name=prowler-app` reports Running + ready (2/2 — api + worker containers)
4. `kubectl logs -n security deploy/prowler-app -c init-db` shows the postgres-init succeeded
5. `kubectl logs -n security deploy/prowler-app -c api` shows `migrate` completed and gunicorn bound on :8080
6. Browse `https://prowler.${SECRET_DOMAIN}/sign-up`, register the first user (becomes tenant owner)
7. In the UI, add a "Kubernetes" provider pointing at the in-cluster ServiceAccount; trigger a scan; confirm findings appear

## Risks & known unknowns

- **First-user signup not locked down**: anyone with internal network access who reaches the UI before you sign up can claim the tenant. Mitigation: register immediately after the UI comes up. Lockdown env var name TBC during implementation (probably `DJANGO_ALLOW_SIGN_UP=False` or similar).
- **DozerDB memory ceiling**: 1G heap is conservative for a homelab; large scans may need bumping. Watch pod restarts on the first big scan.
- **`view` ClusterRole breadth**: gives read on Secrets too. If you prefer not to, swap to a narrower role modeled on upstream's `prowler-role.yaml`.
- **PostgreSQL admin user**: Prowler's `POSTGRES_ADMIN_*` is used for partition management. We point it at the same `prowler` role rather than the super-user, on the assumption it has CREATE TABLE in its own DB. Confirm during first reconcile that pgpartition succeeds; if not, escalate to super-user creds.
- **DJANGO_ALLOWED_HOSTS coverage**: set to `prowler-api,prowler.${SECRET_DOMAIN},prowler.${SECRET_INTERNAL_DOMAIN}`. If Django rejects requests with a different `Host` header (e.g., when envoy forwards under the service DNS), expand the list.
