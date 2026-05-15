# Metabase Deployment Design

## Overview

Deploy [Metabase](https://github.com/metabase/metabase) — an open-source business intelligence / dashboarding platform — into the `database` namespace. Backed by the existing `postgres18` CloudNativePG cluster (no embedded H2), exposed on the internal Envoy Gateway only (both `${SECRET_DOMAIN}` and `${SECRET_INTERNAL_DOMAIN}` hostnames), and instrumented via Metabase's native Prometheus exporter (`MB_PROMETHEUS_SERVER_PORT`, available since 0.49). No PVC: the pod is stateless because all application state lives in Postgres.

Purpose: provide self-service analytics over the cluster's various Postgres datasets (Prowler findings, pgAdmin-managed databases, app DBs) without requiring SQL knowledge for every consumer.

## Scope

**In scope:**

- `metabase` HelmRelease using the bjw-s `app-template` chart (matches every other app in the namespace).
- Dedicated `metabase` Postgres database and role on the existing `postgres18` cluster, bootstrapped by a `postgres-init` initContainer (idempotent, same pattern as Prowler).
- 1Password item `metabase` in the `Talos` vault (created via `op item create`) holding the app role password and the Metabase encryption-secret key. `ExternalSecret` materialises `metabase-secret` with all required `MB_*` and `INIT_POSTGRES_*` keys.
- `HTTPRoute` on `envoy-internal` for `metabase.${SECRET_DOMAIN}` and `metabase.${SECRET_INTERNAL_DOMAIN}`.
- Gatus health monitoring via the `gatus/guarded` component (same as pgAdmin/Prowler).
- `PodMonitor` scraping Metabase's native Prometheus endpoint on port 9191.
- `GrafanaDashboard` (preferred: a maintained community dashboard pulled from grafana.com; fallback: a minimal hand-built JSON covering JVM heap, request rate/latency, DB pool, query counters).

**Out of scope:**

- External (Cloudflare-fronted) exposure. Internal LAN only for now.
- Volsync / PVC. Metabase is stateless when its app DB is external; `/plugins` and `/tmp` use `emptyDir`.
- SSO / LDAP / OIDC integration. First-run wizard creates a local admin; SSO can be layered later (Metabase requires a paid edition for SAML/JWT/LDAP — local users only on OSS).
- Pre-seeded source databases or dashboards. Initial setup is manual through the Metabase UI.
- Email/SMTP configuration. Deferred until alerting is wanted.

## Architecture

```text
                           HTTPRoute (Gateway API)
                  metabase.${SECRET_DOMAIN}
                  metabase.${SECRET_INTERNAL_DOMAIN}
                                │
                                ▼
                  ┌─────────────────────────────┐
                  │ envoy-internal (network ns) │
                  └─────────────┬───────────────┘
                                │  HTTP :3000
                                ▼
   ┌──────────────────────── metabase Pod (database ns) ────────────────────────┐
   │                                                                            │
   │   initContainer: ghcr.io/onedr0p/postgres-init                             │
   │     ── uses INIT_POSTGRES_SUPER_* to CREATE DATABASE metabase /            │
   │        CREATE ROLE metabase (idempotent)                                   │
   │                                                                            │
   │   container: metabase/metabase                                             │
   │     ── envFrom: metabase-secret                                            │
   │     ── port 3000 (http) · port 9191 (metrics)                              │
   │     ── /plugins and /tmp as emptyDir                                       │
   │                                                                            │
   └─────────────────────┬──────────────────────────────────────────┬───────────┘
                         │ postgres                                 │ /metrics
                         ▼                                          ▼
              ┌────────────────────────┐               ┌─────────────────────────┐
              │ postgres18-rw          │               │ PodMonitor → Prometheus │
              │ (CNPG cluster)         │               │  (observability ns)     │
              │  db: metabase          │               └─────────────────────────┘
              │  role: metabase        │                            │
              └────────────────────────┘                            ▼
                         ▲                                ┌─────────────────────┐
                         │ Barman / Kopia                 │ GrafanaDashboard CR │
                         ▼                                └─────────────────────┘
              ┌────────────────────────┐
              │ MinIO / R2 backups     │
              └────────────────────────┘
```

Backups: app data is captured automatically by the existing CNPG backup pipeline (Barman → MinIO + scheduled R2). No app-side backup is required.

## File layout

```text
kubernetes/apps/database/metabase/
├── ks.yaml                          # Flux Kustomization (entry point)
└── app/
    ├── kustomization.yaml           # lists the resources below
    ├── ocirepository.yaml           # bjw-s app-template (OCI)
    ├── externalsecret.yaml          # → metabase-secret (Talos vault)
    ├── helmrelease.yaml             # app-template values
    ├── podmonitor.yaml              # scrape port 9191
    └── grafanadashboard.yaml        # community dashboard import
```

Plus one resources entry added to `kubernetes/apps/database/kustomization.yaml` (alphabetical order, slotted between `dragonfly` and `pgadmin`).

## Components

### Flux Kustomization (`ks.yaml`)

Mirrors `pgadmin/ks.yaml`:

- `dependsOn: cloudnative-pg-cluster` (and implicitly `onepassword` via the global dependency chain).
- `components: [gatus/guarded]`. No `volsync` because there is no PVC.
- `postBuild.substitute.APP: metabase`, `GATUS_SUBDOMAIN: metabase`.
- `wait: false`, `timeout: 5m`, `interval: 1h`, `retryInterval: 2m`.

### OCIRepository

```yaml
url: oci://ghcr.io/bjw-s-labs/helm/app-template
ref:
    tag: 5.0.1 # match other apps; Renovate will keep current
```

### ExternalSecret

1Password item `metabase` in the `Talos` vault. Fields:

| Field                      | Purpose                                                    |
| -------------------------- | ---------------------------------------------------------- |
| `MB_DBUSER`                | Literal `metabase`                                         |
| `MB_DBPASS`                | Random 32-char password for the app role                   |
| `MB_ENCRYPTION_SECRET_KEY` | 32-byte base64 — encrypts source-DB creds stored in app DB |

Created via `op item create --vault Talos --category=Login --title=metabase ...` (per project memory: never paste credentials into the 1Password UI; generate locally with `op`).

The ExternalSecret pulls from two 1Password items, materialising the keys consumed by the container (`MB_*`) and the init container (`INIT_POSTGRES_*`):

```yaml
target:
    name: metabase-secret
    template:
        engineVersion: v2
        data:
            # Metabase application
            MB_DB_TYPE: postgres
            MB_DB_HOST: postgres18-rw.database.svc.cluster.local
            MB_DB_PORT: "5432"
            MB_DB_DBNAME: metabase
            MB_DB_USER: "{{ .MB_DBUSER }}"
            MB_DB_PASS: "{{ .MB_DBPASS }}"
            MB_ENCRYPTION_SECRET_KEY: "{{ .MB_ENCRYPTION_SECRET_KEY }}"
            # postgres-init initContainer
            INIT_POSTGRES_DBNAME: metabase
            INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
            INIT_POSTGRES_USER: "{{ .MB_DBUSER }}"
            INIT_POSTGRES_PASS: "{{ .MB_DBPASS }}"
            INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
            INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
dataFrom:
    - extract:
          key: cloudnative-pg
    - extract:
          key: metabase
```

### HelmRelease

`app-template` values (truncated; final file documents every field):

- **Image**: `docker.io/metabase/metabase` pinned to the current stable tag with `@sha256:…` digest. Tag resolved at implementation time via the registry; Renovate keeps it fresh.
- **initContainers.init-db**: `ghcr.io/onedr0p/postgres-init` with `envFrom: metabase-secret`.
- **containers.app**:
    - `envFrom: metabase-secret`.
    - `env.MB_PROMETHEUS_SERVER_PORT: "9191"`.
    - `env.JAVA_OPTS: "--add-opens java.base/java.nio=ALL-UNNAMED"` (required by Metabase's metrics collector on JDK 17+).
    - `env.TZ: ${TIMEZONE}`.
    - Probes: HTTP `GET /api/health` on `http` (3000). Startup probe `failureThreshold: 60`, `periodSeconds: 5` (Metabase cold start is ~60-90s on first run when it migrates the app DB).
    - Resources: `requests.cpu: 100m`, `requests.memory: 1.5Gi`, `limits.memory: 3Gi`.
- **defaultPodOptions.securityContext**: `runAsNonRoot: true`, `runAsUser: 2000`, `runAsGroup: 2000`, `fsGroup: 2000`, `seccompProfile: { type: RuntimeDefault }`. (UID 2000 matches the upstream Metabase image's default user.)
- **service.app**: ports `http: 3000`, `metrics: 9191`.
- **route.app**: parentRef `envoy-internal/network`, hostnames `{{ .Release.Name }}.${SECRET_DOMAIN}` and `{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}`.
- **persistence**: `plugins` and `tmp` declared as `emptyDir`, mounted at `/plugins` and `/tmp`.

### PodMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
    name: metabase
spec:
    selector:
        matchLabels:
            app.kubernetes.io/name: metabase
    podMetricsEndpoints:
        - port: metrics
          interval: 30s
```

### GrafanaDashboard

`grafana.integreatly.org/v1beta1 GrafanaDashboard`, `instanceSelector` matching the cluster's Grafana instance, importing a community Metabase dashboard from grafana.com. Implementation step: pick the most-recently-revised community dashboard that targets the native `MB_PROMETHEUS_SERVER_PORT` exporter (not legacy JMX-exporter ones). If no maintained option exists, ship a minimal embedded JSON dashboard (configMap-backed, like `flux-instance/app/grafanadashboard.yaml`) covering:

- JVM heap and GC
- HTTP request rate, error rate, latency p50/p95
- Metabase query execution rate and duration
- DB connection pool utilisation

## Data flow

1. Flux reconciles `metabase` Kustomization once `cloudnative-pg-cluster` is Ready.
2. ExternalSecret materialises `metabase-secret` from 1Password.
3. Pod starts; `postgres-init` initContainer creates the `metabase` DB and role using superuser credentials (idempotent — short-circuits on subsequent rollouts).
4. Metabase container starts, runs its internal Liquibase migrations on the empty app DB (first run only), and listens on `:3000` and `:9191`.
5. Envoy-internal HTTPRoute routes `metabase.${SECRET_DOMAIN}` / `metabase.${SECRET_INTERNAL_DOMAIN}` to the Service.
6. Operator visits the URL, completes the setup wizard, adds source databases through the UI.
7. Prometheus scrapes port 9191 every 30s via the PodMonitor; Grafana autoimports the dashboard CR.

## Failure modes & handling

- **`postgres18` unavailable at startup**: init container exits non-zero, Kubernetes restarts the pod with backoff. Once Postgres returns, the next attempt succeeds. No manual intervention.
- **`MB_ENCRYPTION_SECRET_KEY` lost/rotated**: Metabase cannot decrypt source-DB credentials in its app DB. Mitigation: the key lives in 1Password (the canonical store); never rotate it without first running Metabase's `rotate-encryption-key` admin command.
- **Metabase OOM at startup**: First-run Liquibase migration can spike memory. Mitigation: 3Gi memory limit (well above Metabase's documented 1Gi floor) and a generous startup probe.
- **Schema migration failure mid-version-upgrade**: Metabase rolls back on failure but leaves the pod CrashLooping. Mitigation: CNPG's continuous Barman backups and scheduled R2 backups allow a point-in-time recovery of the `metabase` DB if needed.

## Testing & success criteria

Pre-merge:

- `flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster --verbose` passes on the branch.
- `just lint` passes (yamlfmt, super-linter mirror).
- Both `Trivy` checks green on the PR.

Post-apply:

- `kubectl -n database get helmrelease metabase` → Ready=True.
- `kubectl -n database get pod -l app.kubernetes.io/name=metabase` → Running, both init and app containers complete/ready.
- `kubectl -n database exec sts/postgres18 -- psql -U postgres -l` lists the `metabase` database; `\du` lists the `metabase` role.
- `https://metabase.${SECRET_INTERNAL_DOMAIN}` resolves on internal DNS and serves the Metabase setup wizard; `/api/health` returns `{"status":"ok"}`.
- `kubectl -n database port-forward svc/metabase 9191:9191` followed by `curl localhost:9191/metrics` returns Prometheus metrics including `jvm_memory_used_bytes` and `metabase_*` series.
- Prometheus targets page shows the `metabase` PodMonitor as Up.
- Grafana shows the imported dashboard, with non-empty panels for JVM heap.

## Decisions & rationale

- **CNPG over embedded H2** — Metabase's own docs explicitly warn against H2 in production (corruption risk, no concurrent access). The cluster already runs a hardened, replicated, backed-up Postgres; reusing it costs one extra database.
- **bjw-s app-template, not Metabase's own Helm chart** — every other app in this repository uses app-template; consistency wins, and the Metabase requirements (one Deployment + Service + HTTPRoute + initContainer) fit it cleanly. The official Metabase Helm chart adds little for this footprint.
- **Internal-only ingress** — matches the user's stated intent and the namespace's existing posture (pgAdmin, WhoDB, DozerDB are all internal-only). Cloudflare-fronting can be added later by switching the `parentRef` and adding an external hostname.
- **Native Prometheus, not JMX-exporter** — Metabase 0.49+ ships a built-in `/metrics` endpoint; no sidecar or javaagent needed. Fewer moving parts.
- **No `/plugins` PVC** — third-party JDBC drivers are out of scope for the initial deploy. If/when one is needed, switching `plugins` from `emptyDir` to a small PVC is a one-line change.
- **No SMTP / SSO / pre-seeded data** — YAGNI; all of those are configurable after the fact through the UI without redeploying.

## Risks

- **First-run cold start** is slow (Liquibase migrations against an empty DB). The startup probe is sized for it, but if the pod misses the failure threshold, the workaround is to bump `failureThreshold` or `initialDelaySeconds` and re-roll.
- **JVM flag drift**: future Metabase versions may require additional `--add-opens` flags. Easy to spot — pod logs the JVM error on start. Append to `JAVA_OPTS`.
- **Community Grafana dashboard staleness**: if no maintained dashboard exists for the native exporter, the fallback hand-built JSON is small and easy to extend.
