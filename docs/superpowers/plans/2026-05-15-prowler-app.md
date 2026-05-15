# Prowler App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy self-hosted Prowler App (Django API + Next.js UI + Celery worker/beat) into the cluster, backed by existing CNPG postgres18, existing Dragonfly, and a new DozerDB (Neo4j-compatible) StatefulSet. Internal-only ingress on envoy-internal at `prowler.${SECRET_DOMAIN}` / `prowler.${SECRET_INTERNAL_DOMAIN}`.

**Architecture:** Three HelmReleases under `kubernetes/apps/security/prowler/` — `prowler-app` (Deployment with api + worker containers in one Pod sharing emptyDir; init-db creates the Postgres DB), `prowler-ui`, `prowler-beat`. One HelmRelease at `kubernetes/apps/database/dozerdb/` for DozerDB. All wired via the bjw-s app-template chart (`oci://ghcr.io/bjw-s-labs/helm/app-template:5.0.0`), 1Password secrets via ExternalSecret + ClusterSecretStore `onepassword-connect`, gatus `guarded` health check via existing component.

**Tech Stack:** Flux v2, bjw-s app-template 5.0.0, External Secrets Operator + 1Password Connect, CloudNativePG, Dragonfly, Envoy Gateway (Gateway API), DozerDB 5.26.3.0, Prowler 5.26.1.

**Branch:** `feat/prowler-app` (already created and contains the design spec).

**Spec:** `docs/superpowers/specs/2026-05-15-prowler-app-design.md`

---

## Conventions for every YAML file

- Start with `---` separator and a `yaml-language-server: $schema=...` comment, matching the surrounding files in each folder
- Use YAML anchors (`&app`, `&namespace`, `*app`) the same way pocket-id does
- `kubectl` and `flux` commands must be prefixed with `KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig` (or run after `mise install` which auto-exports it via `.mise.toml`)
- All commits are signed-off by user's normal git config; do **not** add Claude attributions

---

## Phase 0 — Prerequisites and recon

### Task 1: Confirm prerequisites and current branch state

**Files:** none.

- [ ] **Step 1: Verify you are on the right branch with a clean working tree.**

```bash
git status
git rev-parse --abbrev-ref HEAD
```

Expected:
- Branch: `feat/prowler-app`
- Working tree clean (or only the existing untracked `scripts/shelly-*` files from before)
- HEAD should be the commit `docs(prowler): co-locate api+worker, drop RWX PVC`

- [ ] **Step 2: Confirm tooling is present.**

```bash
mise install
which op kubectl flux yq jq flux-local
op account list
```

Expected: all binaries resolve; `op account list` shows the user signed into the 1Password account that contains the `Talos` vault.

- [ ] **Step 3: Confirm cluster reachability and verify there is no pre-existing prowler/dozerdb state.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig kubectl get ns security database
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig kubectl get hr -A | grep -E 'prowler|dozerdb' || echo "no pre-existing prowler/dozerdb HelmReleases (expected)"
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig kubectl get sc
```

Expected:
- `security` and `database` namespaces both exist
- No HelmReleases named prowler or dozerdb
- StorageClasses: `ceph-block (default)`, `ceph-bucket`, `openebs-hostpath`

---

## Phase 1 — Bootstrap secrets in 1Password (manual, no commits)

### Task 2: Generate secrets locally and create the two 1Password items

**Files:** none (purely shell + `op` CLI).

- [ ] **Step 1: Generate every secret value into shell variables.**

Run in a single shell so the variables stay in memory:

```bash
SIGNING_KEY="$(openssl genrsa 2048 2>/dev/null)"
VERIFYING_KEY="$(printf '%s' "$SIGNING_KEY" | openssl rsa -pubout 2>/dev/null)"
ENCRYPTION_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
AUTH_SECRET="$(openssl rand -base64 32)"
DBPASS="$(openssl rand -base64 24)"
NEO4J_PASSWORD="$(openssl rand -base64 24)"

# Sanity check (lengths only — never echo the values to anything but your own terminal)
printf 'signing_key bytes: %d\nverifying_key bytes: %d\nencryption_key bytes: %d\nauth_secret bytes: %d\ndbpass bytes: %d\nneo4j_password bytes: %d\n' \
  "${#SIGNING_KEY}" "${#VERIFYING_KEY}" "${#ENCRYPTION_KEY}" "${#AUTH_SECRET}" "${#DBPASS}" "${#NEO4J_PASSWORD}"
```

Expected: all six lengths > 0, signing_key bytes is several hundred (PEM block).

- [ ] **Step 2: Verify neither item already exists in the Talos vault.**

```bash
op item list --vault Talos --categories Login | grep -E '^(prowler|dozerdb)\b' || echo "no existing items (expected)"
```

Expected: no match. If either exists, stop and reconcile — the operator must decide whether to update or recreate.

- [ ] **Step 3: Create the `prowler` 1Password item.**

```bash
op item create --vault Talos --category=Login --title=prowler \
  "PROWLER_DBUSER[text]=prowler" \
  "PROWLER_DBPASS[concealed]=$DBPASS" \
  "DJANGO_TOKEN_SIGNING_KEY[concealed]=$SIGNING_KEY" \
  "DJANGO_TOKEN_VERIFYING_KEY[concealed]=$VERIFYING_KEY" \
  "DJANGO_SECRETS_ENCRYPTION_KEY[concealed]=$ENCRYPTION_KEY" \
  "AUTH_SECRET[concealed]=$AUTH_SECRET"
```

Expected: command prints a `Created Login: prowler` style summary with an item ID.

- [ ] **Step 4: Create the `dozerdb` 1Password item.**

```bash
op item create --vault Talos --category=Login --title=dozerdb \
  "NEO4J_PASSWORD[concealed]=$NEO4J_PASSWORD"
```

Expected: `Created Login: dozerdb` summary.

- [ ] **Step 5: Verify the items are readable back by name (this is how ESO references them).**

```bash
op item get prowler --vault Talos --fields label=PROWLER_DBUSER  # expect: prowler
op item get prowler --vault Talos --fields label=DJANGO_TOKEN_VERIFYING_KEY --reveal | head -1  # expect: -----BEGIN PUBLIC KEY-----
op item get dozerdb --vault Talos --fields label=NEO4J_PASSWORD --reveal >/dev/null && echo OK
```

Expected: `prowler`, `-----BEGIN PUBLIC KEY-----`, `OK`.

- [ ] **Step 6: Clear the shell variables.**

```bash
unset SIGNING_KEY VERIFYING_KEY ENCRYPTION_KEY AUTH_SECRET DBPASS NEO4J_PASSWORD
```

This phase produces no git changes. Move on to Phase 2.

---

## Phase 2 — DozerDB (database namespace)

This phase ends in a single commit: `feat(dozerdb): deploy DozerDB graph database`.

### Task 3: Create `kubernetes/apps/database/dozerdb/app/ocirepository.yaml`

**Files:**
- Create: `kubernetes/apps/database/dozerdb/app/ocirepository.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/source.toolkit.fluxcd.io/ocirepository_v1.json
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: app-template
spec:
  interval: 1h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 5.0.0
  url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

### Task 4: Create `kubernetes/apps/database/dozerdb/app/externalsecret.yaml`

**Files:**
- Create: `kubernetes/apps/database/dozerdb/app/externalsecret.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: dozerdb
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: dozerdb-secret
    template:
      engineVersion: v2
      data:
        # Neo4j-style auth string consumed by the container at startup.
        NEO4J_AUTH: "neo4j/{{ .NEO4J_PASSWORD }}"
  dataFrom:
    - extract:
        key: dozerdb
```

### Task 5: Create `kubernetes/apps/database/dozerdb/app/helmrelease.yaml`

**Files:**
- Create: `kubernetes/apps/database/dozerdb/app/helmrelease.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: &app dozerdb
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: app-template
  install:
    remediation:
      retries: 3
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
      strategy: rollback
  values:
    controllers:
      dozerdb:
        annotations:
          reloader.stakater.com/auto: "true"
        type: statefulset
        containers:
          app:
            image:
              repository: graphstack/dozerdb
              tag: 5.26.3.0@sha256:a77526ea3918fdc46d1fff70c4aea7d71d3874a26ecec059179d6775845b1247
            env:
              TZ: ${TIMEZONE}
              NEO4J_dbms_max__databases: "1000"
              NEO4J_server_memory_pagecache_size: 512M
              NEO4J_server_memory_heap_initial__size: 512M
              NEO4J_server_memory_heap_max__size: 1G
              NEO4J_PLUGINS: '["apoc"]'
              NEO4J_dbms_security_procedures_allowlist: apoc.*
              NEO4J_dbms_security_procedures_unrestricted: ""
              NEO4J_apoc_export_file_enabled: "false"
              NEO4J_apoc_import_file_enabled: "false"
              NEO4J_apoc_import_file_use__neo4j__config: "true"
              NEO4J_apoc_trigger_enabled: "false"
              NEO4J_dbms_connector_bolt_listen__address: 0.0.0.0:7687
            envFrom:
              - secretRef:
                  name: dozerdb-secret
            probes:
              liveness:
                enabled: true
                custom: true
                spec:
                  tcpSocket:
                    port: &port 7687
                  initialDelaySeconds: 30
                  periodSeconds: 30
                  timeoutSeconds: 5
                  failureThreshold: 3
              readiness:
                enabled: true
                custom: true
                spec:
                  tcpSocket:
                    port: *port
                  initialDelaySeconds: 30
                  periodSeconds: 10
                  timeoutSeconds: 5
                  failureThreshold: 3
            resources:
              requests:
                cpu: 100m
                memory: 1536Mi
              limits:
                memory: 2Gi
        statefulset:
          volumeClaimTemplates:
            - name: data
              storageClass: ceph-block
              accessMode: ReadWriteOnce
              size: 10Gi
              globalMounts:
                - path: /data

    defaultPodOptions:
      # DozerDB's entrypoint writes to /var/lib/neo4j inside the image; runs as
      # uid/gid 7474 in upstream Neo4j. We let the image set its own user and
      # only force fsGroup so the PVC is writable.
      securityContext:
        fsGroup: 7474
        fsGroupChangePolicy: OnRootMismatch

    service:
      app:
        controller: dozerdb
        ports:
          bolt:
            port: *port
            protocol: TCP
```

### Task 6: Create `kubernetes/apps/database/dozerdb/app/kustomization.yaml`

**Files:**
- Create: `kubernetes/apps/database/dozerdb/app/kustomization.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./externalsecret.yaml
  - ./helmrelease.yaml
  - ./ocirepository.yaml
```

### Task 7: Create `kubernetes/apps/database/dozerdb/ks.yaml`

**Files:**
- Create: `kubernetes/apps/database/dozerdb/ks.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app dozerdb
  namespace: &namespace database
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  dependsOn:
    - name: external-secrets-stores
      namespace: external-secrets
  interval: 1h
  path: ./kubernetes/apps/database/dozerdb/app
  postBuild:
    substitute:
      APP: *app
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

> **Note on `dependsOn`:** check what other `database/*/ks.yaml` files use. If they reference a different name (e.g. `onepassword-connect` or `external-secrets`), match that exactly. Run:
> ```bash
> grep -A2 "dependsOn:" kubernetes/apps/database/*/ks.yaml
> ```
> and align this `dependsOn` to the same name+namespace used elsewhere in this namespace. If nothing else in `database/` has a dependsOn, remove the block (CNPG itself bootstraps independently).

### Task 8: Register dozerdb in `kubernetes/apps/database/kustomization.yaml`

**Files:**
- Modify: `kubernetes/apps/database/kustomization.yaml`

- [ ] **Step 1: Read the current file to find the correct insertion point.**

```bash
cat kubernetes/apps/database/kustomization.yaml
```

Expected output looks roughly like (entries listed alphabetically under `resources:`):

```yaml
resources:
  - ./namespace.yaml
  - ./netpol.yaml
  - ./cloudnative-pg/ks.yaml
  - ./dragonfly/ks.yaml
  - ./pgadmin/ks.yaml
  - ./whodb/ks.yaml
```

- [ ] **Step 2: Insert `./dozerdb/ks.yaml` alphabetically between `./dragonfly/ks.yaml` and `./pgadmin/ks.yaml`.**

If the file is structured as in Step 1, the resulting `resources:` block should be:

```yaml
resources:
  - ./namespace.yaml
  - ./netpol.yaml
  - ./cloudnative-pg/ks.yaml
  - ./dozerdb/ks.yaml
  - ./dragonfly/ks.yaml
  - ./pgadmin/ks.yaml
  - ./whodb/ks.yaml
```

(Adjust based on what the file actually contains — keep everything else identical, just slot the new line into alphabetical position among the `*/ks.yaml` entries.)

### Task 9: Validate DozerDB locally and commit Phase 2

**Files:** none modified; this task validates and commits the prior tasks.

- [ ] **Step 1: Validate YAML parses.**

```bash
yq eval '.' kubernetes/apps/database/dozerdb/app/ocirepository.yaml >/dev/null && echo ocirepository OK
yq eval '.' kubernetes/apps/database/dozerdb/app/externalsecret.yaml >/dev/null && echo externalsecret OK
yq eval '.' kubernetes/apps/database/dozerdb/app/helmrelease.yaml >/dev/null && echo helmrelease OK
yq eval '.' kubernetes/apps/database/dozerdb/app/kustomization.yaml >/dev/null && echo app-kustomization OK
yq eval '.' kubernetes/apps/database/dozerdb/ks.yaml >/dev/null && echo ks OK
yq eval '.' kubernetes/apps/database/kustomization.yaml >/dev/null && echo db-kustomization OK
```

Expected: six lines, all ending in `OK`.

- [ ] **Step 2: Run flux-local end-to-end test.**

```bash
flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster --verbose 2>&1 | tail -40
```

Expected: tests pass, no errors. If `app-template` chart rendering complains about missing values, re-read the helmrelease.yaml in Task 5 and fix typos; rerun.

- [ ] **Step 3: Stage and commit.**

```bash
git add kubernetes/apps/database/dozerdb/ kubernetes/apps/database/kustomization.yaml
git status   # confirm only the dozerdb files + kustomization.yaml are staged
git diff --cached --stat
git commit -m "$(cat <<'EOF'
feat(dozerdb): deploy DozerDB graph database

Add a single-replica DozerDB (Neo4j-compatible) StatefulSet in the
database namespace backed by a 10Gi ceph-block PVC at /data. Bolt
endpoint on dozerdb.database.svc:7687 with auth pulled from the
"dozerdb" 1Password item via ExternalSecret. Used as the asset-graph
store for Prowler App's attack-paths feature (added in a follow-up
commit).
EOF
)"
```

Expected: pre-commit hooks pass; commit succeeds.

---

## Phase 3 — Prowler App (security namespace)

This phase ends in one commit: `feat(prowler): deploy Prowler App`.

### Task 10: Create `kubernetes/apps/security/prowler/app/ocirepository.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/ocirepository.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/source.toolkit.fluxcd.io/ocirepository_v1.json
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: app-template
spec:
  interval: 1h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 5.0.0
  url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

### Task 11: Create `kubernetes/apps/security/prowler/app/externalsecret.yaml`

This produces `prowler-secret` consumed by **both** the prowler-app HelmRelease (api + worker containers) and the prowler-beat HelmRelease.

**Files:**
- Create: `kubernetes/apps/security/prowler/app/externalsecret.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: prowler
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: prowler-secret
    template:
      engineVersion: v2
      data:
        # ---- Postgres (app connection) ---------------------------------------
        POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
        POSTGRES_PORT: "5432"
        POSTGRES_DB: prowlerdb
        POSTGRES_USER: "{{ .PROWLER_DBUSER }}"
        POSTGRES_PASSWORD: "{{ .PROWLER_DBPASS }}"
        # Prowler's "admin" creds are used for partition mgmt; reuse the app role.
        POSTGRES_ADMIN_USER: "{{ .PROWLER_DBUSER }}"
        POSTGRES_ADMIN_PASSWORD: "{{ .PROWLER_DBPASS }}"
        # ---- init-db (postgres-init initContainer) ---------------------------
        INIT_POSTGRES_DBNAME: prowlerdb
        INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
        INIT_POSTGRES_USER: "{{ .PROWLER_DBUSER }}"
        INIT_POSTGRES_PASS: "{{ .PROWLER_DBPASS }}"
        INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
        INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
        # ---- Valkey / Dragonfly ----------------------------------------------
        VALKEY_HOST: dragonfly.database.svc.cluster.local
        VALKEY_PORT: "6379"
        VALKEY_DB: "0"
        # ---- Neo4j / DozerDB -------------------------------------------------
        NEO4J_HOST: dozerdb.database.svc.cluster.local
        NEO4J_PORT: "7687"
        NEO4J_USER: neo4j
        NEO4J_PASSWORD: "{{ .NEO4J_PASSWORD }}"
        # ---- Django crypto ---------------------------------------------------
        DJANGO_TOKEN_SIGNING_KEY: "{{ .DJANGO_TOKEN_SIGNING_KEY }}"
        DJANGO_TOKEN_VERIFYING_KEY: "{{ .DJANGO_TOKEN_VERIFYING_KEY }}"
        DJANGO_SECRETS_ENCRYPTION_KEY: "{{ .DJANGO_SECRETS_ENCRYPTION_KEY }}"
  dataFrom:
    - extract:
        key: cloudnative-pg
    - extract:
        key: prowler
    - extract:
        key: dozerdb
```

### Task 12: Create `kubernetes/apps/security/prowler/app/externalsecret-ui.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/externalsecret-ui.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: prowler-ui
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: prowler-ui-secret
    template:
      engineVersion: v2
      data:
        # NextAuth signing secret.
        AUTH_SECRET: "{{ .AUTH_SECRET }}"
  dataFrom:
    - extract:
        key: prowler
```

### Task 13: Create `kubernetes/apps/security/prowler/app/rbac.yaml`

The ServiceAccount itself is created by the bjw-s app-template chart (Task 14 declares it under `serviceAccount.prowler: {}` — same convention as `default/homepage/app/helmrelease.yaml`). This file only adds the cluster-scoped binding so that ServiceAccount can read cluster resources for Prowler's Kubernetes provider.

**Files:**
- Create: `kubernetes/apps/security/prowler/app/rbac.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prowler-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: prowler
    namespace: security
```

### Task 14: Create `kubernetes/apps/security/prowler/app/helmrelease-app.yaml`

This is the central HelmRelease: a single Deployment hosting two containers (`api` and `worker`) plus the `init-db` initContainer.

**Files:**
- Create: `kubernetes/apps/security/prowler/app/helmrelease-app.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: &app prowler-app
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: app-template
  install:
    remediation:
      retries: 3
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
      strategy: rollback
  values:
    controllers:
      prowler-app:
        annotations:
          reloader.stakater.com/auto: "true"
        serviceAccount:
          identifier: prowler
        initContainers:
          init-db:
            image:
              repository: ghcr.io/home-operations/postgres-init
              tag: "18@sha256:6fa1f331cddd2eb0b6afa7b8d3685c864127a81ab01c3d9400bc3ff5263a51cf"
            envFrom: &envFrom
              - secretRef:
                  name: prowler-secret
        containers:
          api:
            image: &image
              repository: prowlercloud/prowler-api
              tag: 5.26.1@sha256:6c373234ad95150c761f1ab1b1be56adcaea981f272f55b6490d50fc56245a0f
            env: &commonEnv
              TZ: ${TIMEZONE}
              DJANGO_SETTINGS_MODULE: config.django.production
              DJANGO_BIND_ADDRESS: 0.0.0.0
              DJANGO_PORT: "8080"
              DJANGO_ALLOWED_HOSTS: "prowler-api,prowler.${SECRET_DOMAIN},prowler.${SECRET_INTERNAL_DOMAIN}"
              DJANGO_LOGGING_FORMATTER: ndjson
              DJANGO_LOGGING_LEVEL: INFO
              DJANGO_MANAGE_DB_PARTITIONS: "True"
            envFrom: *envFrom
            probes:
              liveness: &apiProbe
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/v1/
                    port: &port 8080
                  initialDelaySeconds: 60
                  periodSeconds: 30
                  timeoutSeconds: 5
                  failureThreshold: 5
              readiness: *apiProbe
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: false
              capabilities:
                drop:
                  - ALL
            resources:
              requests:
                cpu: 100m
                memory: 512Mi
              limits:
                memory: 1Gi
          worker:
            image: *image
            args: ["worker"]
            env: *commonEnv
            envFrom: *envFrom
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: false
              capabilities:
                drop:
                  - ALL
            resources:
              requests:
                cpu: 100m
                memory: 512Mi
              limits:
                memory: 2Gi

    defaultPodOptions:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault

    serviceAccount:
      prowler: {}

    service:
      app:
        controller: prowler-app
        # Service name "prowler-api" matches what other env vars reference.
        nameOverride: prowler-api
        ports:
          http:
            port: *port

    persistence:
      # Shared scan-output volume — written by `worker`, served by `api`.
      output:
        type: emptyDir
        globalMounts:
          - path: /tmp/prowler_api_output
      # Django config dir (the upstream image writes here at startup).
      config:
        type: emptyDir
        advancedMounts:
          prowler-app:
            api:
              - path: /home/prowler/.config/prowler-api
            worker:
              - path: /home/prowler/.config/prowler-api
      tmp:
        type: emptyDir
        advancedMounts:
          prowler-app:
            api:
              - path: /tmp
                subPath: api-tmp
            worker:
              - path: /tmp
                subPath: worker-tmp
```

> **Note on `service.app.nameOverride`:** the app-template chart names a Service after the HelmRelease (`prowler-app`) unless overridden. The UI's `API_BASE_URL` and Django's `DJANGO_ALLOWED_HOSTS` both expect the service to resolve as `prowler-api`. The `nameOverride: prowler-api` keeps those references valid without renaming the HelmRelease. If app-template 5.0.0 does not expose `nameOverride` on `service.app`, fall back to setting the HelmRelease `metadata.name: prowler-api` and renaming the file to `helmrelease-api.yaml`.

### Task 15: Create `kubernetes/apps/security/prowler/app/helmrelease-ui.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/helmrelease-ui.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: &app prowler-ui
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: app-template
  install:
    remediation:
      retries: 3
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
      strategy: rollback
  values:
    controllers:
      prowler-ui:
        annotations:
          reloader.stakater.com/auto: "true"
        containers:
          app:
            image:
              repository: prowlercloud/prowler-ui
              tag: 5.26.1@sha256:2164d3723857c802c91433c096764c7dbc0d08c1f3f2f9021aaa521be3660cba
            env:
              TZ: ${TIMEZONE}
              UI_PORT: "3000"
              AUTH_URL: "https://prowler.${SECRET_DOMAIN}"
              AUTH_TRUST_HOST: "true"
              API_BASE_URL: "http://prowler-api:8080/api/v1"
              NEXT_PUBLIC_API_BASE_URL: "https://prowler.${SECRET_DOMAIN}/api/v1"
              NEXT_PUBLIC_API_DOCS_URL: "https://prowler.${SECRET_DOMAIN}/api/v1/docs"
            envFrom:
              - secretRef:
                  name: prowler-ui-secret
            probes:
              liveness: &uiProbe
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/health
                    port: &port 3000
                  initialDelaySeconds: 30
                  periodSeconds: 30
                  timeoutSeconds: 5
                  failureThreshold: 5
              readiness: *uiProbe
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: false
              capabilities:
                drop:
                  - ALL
            resources:
              requests:
                cpu: 50m
                memory: 256Mi
              limits:
                memory: 512Mi

    defaultPodOptions:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault

    service:
      app:
        controller: prowler-ui
        ports:
          http:
            port: *port
```

### Task 16: Create `kubernetes/apps/security/prowler/app/helmrelease-beat.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/helmrelease-beat.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: &app prowler-beat
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: app-template
  install:
    remediation:
      retries: 3
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
      strategy: rollback
  values:
    controllers:
      prowler-beat:
        annotations:
          reloader.stakater.com/auto: "true"
        replicas: 1
        # Celery beat is a singleton; force Recreate so two never run at once.
        strategy: Recreate
        containers:
          app:
            image:
              repository: prowlercloud/prowler-api
              tag: 5.26.1@sha256:6c373234ad95150c761f1ab1b1be56adcaea981f272f55b6490d50fc56245a0f
            args: ["beat"]
            env:
              TZ: ${TIMEZONE}
              DJANGO_SETTINGS_MODULE: config.django.production
              DJANGO_LOGGING_FORMATTER: ndjson
              DJANGO_LOGGING_LEVEL: INFO
            envFrom:
              - secretRef:
                  name: prowler-secret
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: false
              capabilities:
                drop:
                  - ALL
            resources:
              requests:
                cpu: 10m
                memory: 64Mi
              limits:
                memory: 128Mi

    defaultPodOptions:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
```

### Task 17: Create `kubernetes/apps/security/prowler/app/httproute.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/httproute.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/gateway.networking.k8s.io/httproute_v1.json
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: prowler
spec:
  parentRefs:
    - name: envoy-internal
      namespace: network
      sectionName: https
  hostnames:
    - "prowler.${SECRET_DOMAIN}"
    - "prowler.${SECRET_INTERNAL_DOMAIN}"
  rules:
    # Prowler REST API — longest-prefix wins, so this beats `/` for /api/v1/*.
    - matches:
        - path:
            type: PathPrefix
            value: /api/v1
      backendRefs:
        - name: prowler-api
          port: 8080
    # Everything else (incl. NextAuth's /api/auth/* and /api/health) → UI.
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: prowler-ui
          port: 3000
```

> **Note on `parentRefs.sectionName`:** check an existing HTTPRoute (e.g. `kubernetes/apps/security/pocket-id/app/httproute.yaml` if present, otherwise any other route) to confirm the convention used in this repo. If routes here don't use `sectionName`, drop it.

### Task 18: Create `kubernetes/apps/security/prowler/app/kustomization.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/app/kustomization.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./externalsecret.yaml
  - ./externalsecret-ui.yaml
  - ./helmrelease-app.yaml
  - ./helmrelease-beat.yaml
  - ./helmrelease-ui.yaml
  - ./httproute.yaml
  - ./ocirepository.yaml
  - ./rbac.yaml
```

### Task 19: Create `kubernetes/apps/security/prowler/ks.yaml`

**Files:**
- Create: `kubernetes/apps/security/prowler/ks.yaml`

- [ ] **Step 1: Create the file.**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app prowler
  namespace: &namespace security
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
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

> **Note on `dependsOn` names:** verify by running
> ```bash
> grep -rh "metadata:" -A1 kubernetes/apps/database/cloudnative-pg/cluster/ks.yaml kubernetes/apps/database/dragonfly/cluster/ks.yaml 2>/dev/null | grep name:
> ```
> and adjusting the names above to match the actual Kustomization `metadata.name` values (the names above are the most common conventions but the repo's actuals are authoritative).

### Task 20: Register prowler in `kubernetes/apps/security/kustomization.yaml`

**Files:**
- Modify: `kubernetes/apps/security/kustomization.yaml`

- [ ] **Step 1: Read the current file.**

```bash
cat kubernetes/apps/security/kustomization.yaml
```

Current `resources:` list is:

```yaml
resources:
  - ./namespace.yaml
  - ./anubis/ks.yaml
  - ./pocket-id/ks.yaml
```

- [ ] **Step 2: Insert `./prowler/ks.yaml` between `anubis` and `pocket-id`.**

Resulting `resources:`:

```yaml
resources:
  - ./namespace.yaml
  - ./anubis/ks.yaml
  - ./prowler/ks.yaml
  - ./pocket-id/ks.yaml
```

Leave the rest of the file (apiVersion, kind, namespace, components) untouched.

### Task 21: Validate Prowler manifests locally and commit Phase 3

**Files:** none modified; validates and commits prior tasks.

- [ ] **Step 1: Validate YAML parses.**

```bash
for f in kubernetes/apps/security/prowler/app/*.yaml kubernetes/apps/security/prowler/ks.yaml kubernetes/apps/security/kustomization.yaml; do
  yq eval '.' "$f" >/dev/null && echo "$f OK" || { echo "$f FAIL"; break; }
done
```

Expected: every line ends `OK`.

- [ ] **Step 2: Run flux-local end-to-end test (covers both DozerDB and Prowler now).**

```bash
flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster --verbose 2>&1 | tail -60
```

Expected: tests pass. Common failures and fixes:
- ClusterRoleBinding fails to find the ServiceAccount because the chart-managed SA hasn't been created yet on first apply. The dependency is implicit: the HelmRelease creates the SA and the raw `rbac.yaml` references it by name. On a fresh install Flux applies both in the same kustomization batch, so the binding may briefly point at a nonexistent SA until the chart reconciles. This resolves itself within seconds; if it persists, re-reconcile the kustomization.
- `nameOverride` not honored on service — fall back to the alternative in Task 14's note (rename HelmRelease to `prowler-api`).

- [ ] **Step 3: Stage and commit.**

```bash
git add kubernetes/apps/security/prowler/ kubernetes/apps/security/kustomization.yaml
git status
git diff --cached --stat
git commit -m "$(cat <<'EOF'
feat(prowler): deploy Prowler App self-hosted

Three HelmReleases under security/prowler/:
- prowler-app: Deployment with `api` (gunicorn) + `worker` (celery)
  containers sharing an emptyDir at /tmp/prowler_api_output, plus an
  init-db initContainer that bootstraps the prowlerdb database/role
  against the existing CNPG postgres18 cluster.
- prowler-ui: Next.js frontend with NextAuth.
- prowler-beat: celery beat scheduler (singleton, Recreate strategy).

Reuses CNPG postgres18 and Dragonfly; talks to the new dozerdb
StatefulSet for the asset-graph feature. Internal-only ingress on
envoy-internal at prowler.${SECRET_DOMAIN} and prowler.${SECRET_INTERNAL_DOMAIN}.
Gatus guarded health check via the existing component.
EOF
)"
```

Expected: pre-commit hooks pass; commit succeeds.

---

## Phase 4 — Deploy and verify

### Task 22: Push and reconcile

**Files:** none.

- [ ] **Step 1: Push the branch.**

```bash
git push -u origin feat/prowler-app
```

Expected: branch pushed; remote ref created.

- [ ] **Step 2: Force-reconcile the Flux git source so the cluster sees the new commits without waiting for the next interval.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile source git flux-system

KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile kustomization external-secrets -n external-secrets
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile kustomization onepassword -n external-secrets
```

> Note: this pushes to the feature branch, which Flux on the cluster will **not** pull from automatically. The cluster tracks `main`. The reconciles above are still useful — once the PR merges and Flux's git source picks up the new commits on main, the subsequent reconciles in Steps 3–5 will work without further nudging.

- [ ] **Step 3: Once merged to main, force a fresh sync.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile source git flux-system

KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile kustomization dozerdb -n database

KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  flux reconcile kustomization prowler -n security
```

Expected: each command returns within ~30s with "applied" or "ready".

### Task 23: Verify DozerDB

**Files:** none.

- [ ] **Step 1: Watch the StatefulSet come up.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl rollout status sts/dozerdb -n database --timeout=5m
```

Expected: `statefulset rolling update complete`. If it hangs, look at:

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig kubectl describe sts dozerdb -n database
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig kubectl logs sts/dozerdb -n database --tail=200
```

- [ ] **Step 2: Confirm Bolt port responds.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl run -n database --rm -i --restart=Never bolt-probe \
  --image=busybox:1.37.0 -- sh -c 'echo > /dev/tcp/dozerdb/7687 && echo "bolt reachable"'
```

Expected: `bolt reachable`. (If the image refuses TCP redirect, fall back to `nc -zv dozerdb 7687`.)

### Task 24: Verify Prowler

**Files:** none.

- [ ] **Step 1: Watch each Deployment.**

```bash
for d in prowler-app prowler-ui prowler-beat; do
  KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
    kubectl rollout status deploy/$d -n security --timeout=10m
done
```

Expected: each prints `deployment "<name>" successfully rolled out`. The `prowler-app` rollout will take longest because the init-db must finish before the api can run migrations.

- [ ] **Step 2: Confirm init-db succeeded.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl logs deploy/prowler-app -n security -c init-db
```

Expected: lines like `CREATE ROLE` / `CREATE DATABASE` / `done` with no errors. (Running it twice is idempotent — re-runs print "already exists" notices.)

- [ ] **Step 3: Confirm migrations ran and gunicorn is listening.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl logs deploy/prowler-app -n security -c api --tail=200 | grep -E "(Applying|Listening|Booting|gunicorn|error|ERROR)" | head -30
```

Expected: a sequence of `Applying contenttypes.0001_initial... OK` lines, ending with `Listening at: http://0.0.0.0:8080`.

- [ ] **Step 4: Confirm worker connected to broker.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl logs deploy/prowler-app -n security -c worker --tail=200 | grep -E "(celery|ready|connected|error|ERROR)" | head -20
```

Expected: `celery@... ready` and a `Connected to redis://dragonfly...` (or similar) line.

- [ ] **Step 5: Confirm beat scheduler is firing.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl logs deploy/prowler-beat -n security --tail=100 | grep -E "(beat:|Scheduler|ready|error)" | head -10
```

Expected: `beat: Starting...` and `Scheduler: Sending due task` or similar.

- [ ] **Step 6: Confirm gatus has the new endpoint and it's green.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl get configmap -n observability -l app.kubernetes.io/name=gatus -o yaml \
  | grep -A2 prowler
```

Expected: a gatus endpoint config for `prowler` pointing at the UI or API. Hit the gatus dashboard via your usual URL and confirm the prowler check is green within ~2 minutes.

### Task 25: First-user signup and smoke-test Kubernetes scan

**Files:** none.

- [ ] **Step 1: Browse to the UI and sign up the first user.**

Open `https://prowler.${SECRET_DOMAIN}/sign-up` (substitute your actual domain). Fill in the form. The first registered user becomes the tenant owner.

- [ ] **Step 2: Add a "Kubernetes" provider in the UI.**

In the UI: **Providers → Add provider → Kubernetes**. Use the in-cluster ServiceAccount option. The exact wording differs between Prowler versions; pick the option that says "use in-cluster credentials" or "service account". If only "kubeconfig upload" is offered, generate a kubeconfig that authenticates as the `prowler` ServiceAccount:

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl create token prowler -n security --duration=8760h
```

Use that token in a kubeconfig pointing at `https://kubernetes.default.svc.cluster.local` and upload via the UI.

- [ ] **Step 3: Trigger a scan and wait for findings.**

Kick off a scan from the UI. It typically completes in a few minutes for a small homelab cluster. Confirm findings appear in the dashboard.

- [ ] **Step 4: Sanity-check resource use.**

```bash
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl top pod -n security -l 'app.kubernetes.io/name in (prowler-app, prowler-ui, prowler-beat)'
KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig \
  kubectl top pod -n database -l app.kubernetes.io/name=dozerdb
```

Expected: nothing near memory limits, no pod restarts.

### Task 26: Save lessons-learned to memory (only if notable)

**Files:**
- Possibly create: `/Users/luke.evans/.claude/projects/-Users-luke-evans-GIT-LukeEvansTech-talos-cluster/memory/project_prowler_deploy.md`
- Possibly modify: `/Users/luke.evans/.claude/projects/-Users-luke-evans-GIT-LukeEvansTech-talos-cluster/memory/MEMORY.md`

- [ ] **Step 1: Capture only the non-obvious lessons from the deploy.**

Examples worth saving (only if they actually came up):
- bjw-s app-template's exact syntax for multi-container deployments + shared `emptyDir`
- Working value for Prowler's `POSTGRES_ADMIN_*` (did pointing it at the app user work, or did partitioning fail and force escalation to super-user?)
- Whether `view` ClusterRole was sufficient for K8s scans
- Any startup-order quirks (e.g. did beat need a longer initial delay because the api migration was slow?)

Do **not** save: file paths, env var lists, generic Helm patterns — those live in the repo and the spec already.

- [ ] **Step 2: If memorable, write the file and add an index entry to MEMORY.md.**

Follow the format used in existing `project_*.md` files (e.g. `project_keda_nfs_scaler_blackbox_dns_flap.md`): YAML frontmatter, then a brief markdown body covering "what happened, why, how to apply." Add a single line under `## Projects` in `MEMORY.md` linking to it.

If nothing notable came up, skip this task entirely — empty memory is better than noisy memory.

---

## Phase 5 — Open the PR

### Task 27: Open PR against `main`

**Files:** none (uses `gh`).

- [ ] **Step 1: Inspect the commits the PR will contain.**

```bash
git log --oneline main..HEAD
git diff main...HEAD --stat
```

Expected: three commits — the docs/spec one, `feat(dozerdb): ...`, `feat(prowler): ...`. Stat shows the new files under `kubernetes/apps/database/dozerdb/`, `kubernetes/apps/security/prowler/`, `docs/superpowers/{specs,plans}/`, and the two `kustomization.yaml` registrations.

- [ ] **Step 2: Create the PR.**

```bash
gh pr create --base main --head feat/prowler-app \
  --title "feat: deploy Prowler App self-hosted" \
  --body "$(cat <<'PRBODY'
## Summary
- Adds Prowler App (api + worker in one Pod, ui, beat) under security/prowler/
- Adds DozerDB graph database under database/dozerdb/ (asset-graph backend Prowler requires)
- Reuses existing CNPG postgres18 and Dragonfly; new prowlerdb database/role bootstrapped via the postgres-init initContainer pattern
- Internal-only ingress on envoy-internal
- Gatus guarded health check enabled
- Design: docs/superpowers/specs/2026-05-15-prowler-app-design.md
- Plan: docs/superpowers/plans/2026-05-15-prowler-app.md

## Test plan
- [ ] flux-local test passes in CI
- [ ] security-scans (Checkov + Trivy) passes
- [ ] After merge, flux get hr -n database shows dozerdb ready
- [ ] After merge, flux get hr -n security shows prowler-app, prowler-ui, prowler-beat ready
- [ ] init-db logs show prowlerdb + prowler role created
- [ ] api logs show migrations applied + gunicorn listening
- [ ] worker logs show celery ready + broker connected
- [ ] beat logs show scheduler starting
- [ ] Sign-up flow at the internal prowler URL works
- [ ] First Kubernetes scan completes and findings appear in the UI
PRBODY
)"
```

Expected: gh prints the new PR URL. Open it in a browser; confirm both `flux-local` and `security-scans` workflows run and pass.

- [ ] **Step 3: Address any CI feedback.**

If `security-scans` flags new Trivy findings, evaluate whether they're real:
- True positive in our code → fix it
- False positive (e.g. Prowler image's own dependencies) → add a path-scoped entry to `.trivyignore.yaml` at the repo root and re-push

If `flux-local` fails, read the error, fix the manifest, push again — do not amend (per repo CLAUDE.md).

---

## Spec coverage check

Cross-referencing this plan against `docs/superpowers/specs/2026-05-15-prowler-app-design.md`:

| Spec section                  | Tasks covering it                                       |
| ----------------------------- | ------------------------------------------------------- |
| Architecture overview         | Tasks 5, 14, 15, 16                                     |
| Repo layout                   | Tasks 3–8, 10–20                                        |
| dozerdb component detail      | Tasks 3, 4, 5                                           |
| prowler-app component detail  | Task 14                                                 |
| prowler-ui component detail   | Task 15                                                 |
| prowler-beat component detail | Task 16                                                 |
| Networking / HTTPRoute        | Task 17                                                 |
| RBAC                          | Task 13                                                 |
| Secrets (op CLI + ES)         | Tasks 2, 4, 11, 12                                      |
| cluster-secrets reuse         | Used in Tasks 14, 15 (no new keys)                      |
| Flux Kustomization (`ks.yaml`)| Tasks 7, 19                                             |
| Verification                  | Tasks 23, 24, 25                                        |
| Risks / known unknowns        | Tasks 14 (nameOverride), 21 (failure modes), 25 (first-user) |

No spec sections are uncovered.



