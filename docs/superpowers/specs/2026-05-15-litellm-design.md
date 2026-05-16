# LiteLLM Proxy Design

## Overview

Deploy [LiteLLM](https://github.com/BerriAI/litellm) proxy in front of the existing Ollama instance, alongside an OpenRouter passthrough. Run the **full proxy** with a Postgres backend so we exercise virtual keys, per-key budgets, request logging, and the Admin UI — the features that make LiteLLM more than a thin wrapper.

Purpose: learn the LLM-gateway pattern on a low-stakes deployment before considering it for real consumers (n8n, Open-WebUI, future MCP/agent work). Existing apps are not migrated in this change.

## Scope

**In scope:**

- New app at `kubernetes/apps/ai/litellm/` using bjw-s `app-template`
- New database `litellm` and role `litellm` on the existing `postgres18` CNPG cluster (created on first start by the `postgres-init` initContainer pattern, same as `tandoor`)
- Reuses the existing `dragonfly.database.svc.cluster.local` for response caching
- ConfigMap-driven `config.yaml` with two seed models (Ollama + OpenRouter) and `STORE_MODEL_IN_DB: True` so more models can be added through the UI
- ExternalSecret pulling a new `litellm` item from 1Password (vault `Talos`) plus the existing `cloudnative-pg` item for superuser credentials
- Internal-only HTTPRoute on `envoy-internal` at `litellm.${SECRET_INTERNAL_DOMAIN}`
- Prometheus ServiceMonitor on `/metrics` + GrafanaDashboard CR importing LiteLLM's published dashboard JSON
- Gatus `guarded` health check via the existing component

**Out of scope (deferred):**

- Langfuse / Phoenix / Helicone — external request tracing. Prometheus + Grafana cover the metrics surface; Langfuse adds a second app + Postgres DB that doubles scope.
- OIDC SSO in front of the Admin UI. Built-in `UI_USERNAME` / `UI_PASSWORD` is sufficient on the internal network.
- Public exposure via `envoy-external` / Cloudflare tunnel.
- Migrating Open-WebUI or n8n to call LiteLLM. They keep their existing direct connections to Ollama / their own providers.
- HA / multi-replica. Single replica with `Recreate` strategy is appropriate for a learning deployment and avoids rolling-update races against Prisma DB migrations.
- VolSync backup. The proxy keeps no state on disk (PVC-less); state lives in Postgres (already covered by Barman/R2) and config lives in Git.
- Per-consumer virtual keys with budgets. Provisioning those is a runtime exercise via the UI after deployment, not a manifest change.
- CiliumNetworkPolicy specific to LiteLLM. The namespace-level `allow-cross-namespace-ingress` policy in `ai/netpol.yaml` already covers ingress from the gateway.

## Architecture

```text
┌─ namespace: ai ────────────────────────────────────────────────────────┐
│                                                                        │
│   ollama        existing       :11434                                  │
│   open-webui    existing       → talks directly to ollama (unchanged)  │
│                                                                        │
│   litellm       NEW Deployment, 1 replica                              │
│   ┌─ Pod ──────────────────────────────────────────────────────────┐  │
│   │  initContainer: init-db (postgres-init:18) creates DB + role   │  │
│   │  container: app  ghcr.io/berriai/litellm  :4000                │  │
│   │    args: --config=/app/config.yaml --port=4000                 │  │
│   │    config.yaml mounted from configMap "litellm-configmap"      │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
       │                              │                            │
       ▼                              ▼                            ▼
┌─ namespace: database ───────────────────────────┐    ┌─ egress ──────────┐
│   postgres18-rw  CNPG :5432                     │    │  openrouter.ai    │
│      db: litellm, role: litellm                 │    │  (provider calls) │
│   dragonfly     :6379                            │    └───────────────────┘
│      cache, success/failure callback queues     │
└─────────────────────────────────────────────────┘

ingress: envoy-internal
  HTTPRoute litellm.${SECRET_INTERNAL_DOMAIN}
    /        → litellm:4000   (proxy API + Admin UI on same port)
```

The Admin UI and the OpenAI-compatible API share port 4000. The UI lives at `/ui`, the API at `/v1/*`. Both are reachable through the same HTTPRoute on the internal listener.

## Repository layout

```text
kubernetes/apps/ai/litellm/
├── ks.yaml                          # Flux Kustomization; depends on cnpg-cluster, dragonfly-cluster, onepassword
└── app/
    ├── kustomization.yaml
    ├── ocirepository.yaml           # bjw-s app-template chart
    ├── helmrelease.yaml             # 1 replica, Recreate, postgres-init initContainer, envoy-internal route
    ├── configmap.yaml               # litellm-configmap, holds config.yaml
    ├── externalsecret.yaml          # litellm-secret from 1Password "litellm" + "cloudnative-pg"
    ├── servicemonitor.yaml          # scrape :4000/metrics, 30s interval
    └── grafanadashboard.yaml        # GrafanaDashboard CR referencing LiteLLM's published JSON
```

Plus one line added to `kubernetes/apps/ai/kustomization.yaml` to register `./litellm/ks.yaml`. Alphabetical order places it before `ollama`.

## Components & dependencies

The `ks.yaml` uses:

- `components/gatus/guarded` — internal-only health check, matches `ollama`
- **No** `components/volsync` — there is no PVC
- `dependsOn` (all Kustomizations live in the `flux-system` namespace per repository convention):
    - `cloudnative-pg-cluster` — so `postgres18-rw` is healthy
    - `dragonfly-cluster` — so the cache backend is up
    - `onepassword-connect` — for the ExternalSecret

## Secret & data flow

### 1Password item

Vault: `Talos`. Item: `litellm`. Created via `op item create --vault Talos`, never the UI. Fields:

| Field                   | Purpose                                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LITELLM_MASTER_KEY`    | `sk-`-prefixed long random string. Root API key; also the bootstrap admin identity in the UI.                                                            |
| `LITELLM_SALT_KEY`      | 32-byte random. Encrypts provider keys stored in Postgres. **Cannot be rotated without losing UI-added provider keys** — record this in 1Password notes. |
| `LITELLM_UI_USERNAME`   | Admin UI login                                                                                                                                           |
| `LITELLM_UI_PASSWORD`   | Admin UI password                                                                                                                                        |
| `LITELLM_POSTGRES_USER` | Per-app role name, e.g. `litellm`                                                                                                                        |
| `LITELLM_POSTGRES_PASS` | Random                                                                                                                                                   |
| `OPENROUTER_API_KEY`    | From openrouter.ai, prefixed `sk-or-...`                                                                                                                 |

The existing 1Password item `cloudnative-pg` already provides `POSTGRES_SUPER_USER` / `POSTGRES_SUPER_PASS` for the init container.

### ExternalSecret

Pulls both 1Password items and templates a single `litellm-secret`. Same shape as `tandoor`'s ExternalSecret:

```yaml
target:
    name: litellm-secret
    template:
        engineVersion: v2
        data:
            DATABASE_URL: postgresql://{{ .LITELLM_POSTGRES_USER }}:{{ .LITELLM_POSTGRES_PASS }}@postgres18-rw.database.svc.cluster.local:5432/litellm?sslmode=require
            LITELLM_MASTER_KEY: "{{ .LITELLM_MASTER_KEY }}"
            LITELLM_SALT_KEY: "{{ .LITELLM_SALT_KEY }}"
            UI_USERNAME: "{{ .LITELLM_UI_USERNAME }}"
            UI_PASSWORD: "{{ .LITELLM_UI_PASSWORD }}"
            OPENROUTER_API_KEY: "{{ .OPENROUTER_API_KEY }}"
            INIT_POSTGRES_DBNAME: litellm
            INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
            INIT_POSTGRES_USER: "{{ .LITELLM_POSTGRES_USER }}"
            INIT_POSTGRES_PASS: "{{ .LITELLM_POSTGRES_PASS }}"
            INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
            INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
dataFrom:
    - extract: { key: litellm }
    - extract: { key: cloudnative-pg }
```

### First-start flow

1. Flux reconciles → ExternalSecret materialises `litellm-secret`
2. Pod scheduled. `init-db` initContainer (`ghcr.io/home-operations/postgres-init:18`) runs against `postgres18-rw` using the super credentials, idempotently creates role `litellm` and database `litellm`
3. Main container starts. LiteLLM runs Prisma migrations against `DATABASE_URL`, comes up on port 4000
4. ServiceMonitor begins scraping `/metrics` on the next interval (30s)
5. GrafanaDashboard CR is picked up by the Grafana operator and appears in Grafana

## config.yaml

ConfigMap `litellm-configmap` carries the proxy config. Seeded with two models, `STORE_MODEL_IN_DB: True` set in env so additional models added via the UI persist to Postgres and merge with the file list.

```yaml
model_list:
    - model_name: ollama/llama3 # alias clients see
      litellm_params:
          model: ollama_chat/llama3 # actual ollama call
          api_base: http://ollama.ai.svc.cluster.local:11434
    - model_name: openrouter/auto
      litellm_params:
          model: openrouter/openrouter/auto
          api_key: os.environ/OPENROUTER_API_KEY

router_settings:
    redis_host: os.environ/REDIS_HOST
    redis_port: os.environ/REDIS_PORT

litellm_settings:
    success_callback: ["prometheus"]
    failure_callback: ["prometheus"]
    cache: true
    cache_params:
        type: redis
        host: os.environ/REDIS_HOST
        port: os.environ/REDIS_PORT
        ttl: 30
    drop_params: true
    num_retries: 2
    request_timeout: 600

general_settings:
    health_check_endpoint: /v1/health
```

Notes:

- `os.environ/<NAME>` is LiteLLM syntax for "read this env var at request time" — keeps the OpenRouter key out of Git and out of the rendered ConfigMap.
- `prometheus` callbacks emit per-request counters/histograms scraped by the ServiceMonitor.
- `drop_params: true` silently drops parameters a backend doesn't support (e.g. some Ollama models don't accept `tool_choice`).
- `cache.ttl: 30` is short on purpose — long TTLs make per-request usage tracking misleading; LiteLLM caches identical requests for 30s which is enough to deduplicate burst traffic from things like agent loops.

## HelmRelease shape

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: { name: &app litellm }
spec:
    interval: 1h
    chartRef: { kind: OCIRepository, name: app-template }
    install: { remediation: { retries: 3 } }
    upgrade:
        { cleanupOnFail: true, remediation: { retries: 3, strategy: rollback } }
    values:
        controllers:
            litellm:
                annotations: { reloader.stakater.com/auto: "true" }
                replicas: 1
                strategy: Recreate
                initContainers:
                    init-db:
                        image:
                            repository: ghcr.io/home-operations/postgres-init
                            tag: "18@sha256:<pinned>"
                        envFrom: [{ secretRef: { name: litellm-secret } }]
                containers:
                    app:
                        image:
                            repository: ghcr.io/berriai/litellm
                            tag: main-stable@sha256:<pinned>
                        args: ["--config=/app/config.yaml", "--port=4000"]
                        env:
                            LITELLM_LOG: INFO
                            LITELLM_MODE: PRODUCTION
                            STORE_MODEL_IN_DB: "True"
                            REDIS_HOST: dragonfly.database.svc.cluster.local
                            REDIS_PORT: "6379"
                        envFrom: [{ secretRef: { name: litellm-secret } }]
                        probes:
                            liveness: &probe
                                enabled: true
                                custom: true
                                spec:
                                    httpGet:
                                        { path: /health/liveliness, port: 4000 }
                                    initialDelaySeconds: 30
                                    periodSeconds: 10
                            readiness: *probe
                        resources:
                            requests: { cpu: 100m, memory: 512Mi }
                            limits: { memory: 2Gi }
        service:
            app:
                controller: litellm
                ports: { http: { port: 4000 } }
        route:
            app:
                hostnames: ["{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}"]
                parentRefs: [{ name: envoy-internal, namespace: network }]
        persistence:
            config:
                type: configMap
                name: litellm-configmap
                globalMounts:
                    - {
                          path: /app/config.yaml,
                          subPath: config.yaml,
                          readOnly: true,
                      }
```

Notable choices:

- **`strategy: Recreate`** — with one replica running Prisma migrations on startup, avoids the brief window where an old pod talks to a freshly-migrated schema. Easy to revert to RollingUpdate once we run multiple replicas.
- **Hostname on `envoy-internal` only** — no public exposure. The Admin UI ships with a username/password login, but the broader security surface (UI vulns, leaked master key) is not worth Cloudflare-exposing for a learning deployment.
- **Single hostname** — `litellm.${SECRET_INTERNAL_DOMAIN}` only. We don't dual-stack with `${SECRET_DOMAIN}` because LiteLLM is not intended for external clients in this iteration.
- **Liveness/readiness path `/health/liveliness`** — LiteLLM's actual liveness endpoint. (Yes, `liveliness` not `liveness`; that's the upstream spelling.) `/v1/health` requires authentication and runs a real backend probe — too heavy for kubelet.

## Observability

### ServiceMonitor

Standard pattern. Selects on the `app.kubernetes.io/name: litellm` label that `commonMetadata` applies via the Flux Kustomization.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
    name: litellm
    labels: { app.kubernetes.io/name: litellm }
spec:
    selector:
        matchLabels: { app.kubernetes.io/name: litellm }
    namespaceSelector: { matchNames: [ai] }
    endpoints:
        - { port: http, path: /metrics, interval: 30s, scrapeTimeout: 10s }
```

### GrafanaDashboard

Reference LiteLLM's published dashboard JSON. Follows the same pattern as the `flux-system` / `gpu-operator` dashboards — a `GrafanaDashboard` CR pointing at a sibling ConfigMap holding the JSON body, with `allowCrossNamespaceImport: true` so the operator picks it up.

Any Grafana template variables in the JSON (`${datasource}`, `${interval}`, etc.) must be escaped to `$${...}` so Flux `postBuild` doesn't strip them. Set `"datasource": null` rather than a hardcoded UID so the default Prometheus datasource resolves correctly.

### Gatus

`components/gatus/guarded` adds an internal Gatus endpoint that probes `https://litellm.${SECRET_INTERNAL_DOMAIN}` and alerts via the existing AlertManager wiring.

## Verification plan

After Flux reconciles:

1. `kubectl -n ai get hr litellm` → `Ready: True`
2. `kubectl -n ai logs deploy/litellm -c init-db` → exits 0; role + DB created
3. `kubectl -n ai logs deploy/litellm -c app` → Prisma "Migration applied"; server listening on `0.0.0.0:4000`
4. Browse to `https://litellm.${SECRET_INTERNAL_DOMAIN}/ui`, log in with `UI_USERNAME` / `UI_PASSWORD`
5. Call the API directly:

    ```bash
    curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
         -d '{"model":"ollama/llama3","messages":[{"role":"user","content":"hi"}]}' \
         https://litellm.${SECRET_INTERNAL_DOMAIN}/v1/chat/completions
    ```

    Should round-trip to Ollama.

6. Repeat with `model: openrouter/auto` — should round-trip to OpenRouter and consume tokens there.
7. Grafana → LiteLLM dashboard shows the two requests above.
8. Gatus shows `litellm` green within the next interval.

## Risks & mitigations

- **OpenRouter spend.** Once a virtual key is created and handed to a consumer, runaway agent loops can burn credit fast. Mitigated by (a) keeping consumers off LiteLLM initially and (b) using the UI's per-key `max_budget` once a real consumer is introduced. Out of scope for the deployment itself.
- **Salt key loss.** Rotating `LITELLM_SALT_KEY` makes UI-added provider keys undecipherable. Mitigated by storing it in 1Password and noting in the item that it must never change. The config-file-defined OpenRouter key in `config.yaml` is unaffected (not stored in DB).
- **Postgres tenant noise.** LiteLLM joins five other apps on `postgres18`. Mitigated by `postgres18`'s 300-connection ceiling and the proxy's small connection pool. Re-evaluate if logging volume balloons.
- **Stale-schema on rolling update.** Avoided by `strategy: Recreate` + 1 replica. If we later scale replicas, LiteLLM's docs call out running migrations as a separate Job; revisit then.
