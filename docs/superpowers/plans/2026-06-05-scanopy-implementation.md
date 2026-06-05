# Scanopy Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Scanopy (network-documentation tool) into `kubernetes/apps/network/scanopy/` as a Flux-managed app: a server+UI Deployment backed by the shared CNPG cluster, plus a privileged hostNetwork DaemonSet scanner on every node.

**Architecture:** Two HelmReleases (`scanopy` server, `scanopy-daemon` scanner) share one `app-template` OCIRepository. The server provisions its DB via a `postgres-init` initContainer against `postgres18-rw.database.svc` (TLS `sslmode=require`), persists `/data` on a volsync-backed ceph-block PVC, and is exposed on `envoy-internal`. The daemon runs `hostNetwork`+`privileged` for L2/SNMP reach, registers to the server over the cluster Service, and reads the SNMP community from a mounted ExternalSecret. Secrets come from the 1Password `Talos` vault via the `onepassword-connect` ClusterSecretStore.

**Tech Stack:** Talos · Kubernetes · Flux CD · bjw-s `app-template` 5.0.1 · CloudNativePG · External Secrets (1Password) · cert-manager · Gateway API (Envoy) · VolSync · Gatus.

---

## Confirmed facts (verified against the live repo, 2026-06-05)

These resolve every "confirm/verify" note from the design spec. Do **not** re-litigate them:

| Item                       | Confirmed value                                                                                                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClusterSecretStore         | `onepassword-connect` (kind `ClusterSecretStore`)                                                                                                                                                             |
| CNPG Flux dependency       | `name: cloudnative-pg-cluster`, `namespace: database`                                                                                                                                                         |
| `onepassword` dependsOn    | **Not used** by netbox/metabase — omit it (apps rely on the store being ready)                                                                                                                                |
| CNPG superuser keys        | The `cloudnative-pg` 1Password item exposes `POSTGRES_SUPER_USER` + `POSTGRES_SUPER_PASS` (metabase consumes both in production)                                                                              |
| DB TLS                     | CNPG presents a valid cert-manager cert (`postgres18-tls`, real SANs/issuer). Use `?sslmode=require` (matches NetBox). **Not** `disable` — that is a JVM-only Metabase workaround                             |
| app-template               | `oci://ghcr.io/bjw-s-labs/helm/app-template` tag `5.0.1` (147 apps), with the `layerSelector` block                                                                                                           |
| postgres-init image        | `ghcr.io/home-operations/postgres-init:18@sha256:5086f94abc783f1147d7c2a32c01db00ab594820026e4f6a82ac2af3dbde7fc7` (reuse NetBox's pin)                                                                       |
| Gateway                    | `envoy-internal` exists in the `network` namespace (`envoy-gateway/app/envoy.yaml:85`)                                                                                                                        |
| Namespace PSA              | `network/namespace.yaml` has **no** PodSecurity labels → privileged pods allowed                                                                                                                              |
| volsync component          | `kubernetes/components/volsync`; creates PVC named `${APP}`; needs `APP` + `VOLSYNC_CAPACITY` (defaults `ceph-block` / `5Gi`)                                                                                 |
| gatus/guarded              | DNS-based check keyed on `${GATUS_SUBDOMAIN}.${SECRET_DOMAIN}` — **not** an HTTP probe. Needs `GATUS_SUBDOMAIN` in postBuild. (metabase pattern; the route-annotation HTTP form is an optional later upgrade) |
| network kustomization slot | Insert `./scanopy/ks.yaml` between `./opnsense-dns/ks.yaml` and `./tailscale-operator/ks.yaml`                                                                                                                |
| Image digests              | server `v0.16.2@sha256:e3c55cd639b9643c01e21c1fcc5470c6524ea5c3ac316768bf028737c656bbe6` · daemon `v0.16.2@sha256:98cdba02b2b127462cba87c10dacdc5671354e2dd43a124b72f8cfd2dfb4b911`                           |

**Deviations from the design spec, locked here:**

1. **Drop the `onepassword` dependsOn** — non-idiomatic in this repo.
2. **Drop the `gatus.home-operations.com/endpoint` route annotation** — the locked `gatus/guarded` component is a DNS check; mixing in an HTTP annotation creates a second, redundant endpoint. Keep `gatus/guarded` + `GATUS_SUBDOMAIN: scanopy` only.
3. **DB URL uses `?sslmode=require`** (per the corrected spec).

---

## File structure

```
kubernetes/apps/network/scanopy/
├── ks.yaml                      # Flux Kustomization (network ns); components volsync + gatus/guarded
└── app/
    ├── kustomization.yaml       # lists the 5 resources
    ├── ocirepository.yaml       # app-template 5.0.1, name: scanopy (shared by both HRs)
    ├── externalsecret.yaml      # scanopy-secret  (DB init + connection + daemon pairing)
    ├── externalsecret-snmp.yaml # scanopy-snmp-secret (SNMP community)
    ├── helmrelease.yaml         # scanopy — server + UI (Deployment)
    └── daemon-helmrelease.yaml  # scanopy-daemon — scanner (privileged hostNetwork DaemonSet)
```

Plus one line added to `kubernetes/apps/network/kustomization.yaml`.

---

## Prerequisites (manual — do before applying; not committable)

- [ ] **P1: Create the 1Password `scanopy` item in the `Talos` vault.**

The repo is PUBLIC and the ClusterSecretStore only reads the `Talos` vault. Never paste secrets in the 1Password UI — use the CLI. `SNMP_COMMUNITY` is the one value only Luke can supply.

```bash
op item create --vault Talos --title scanopy --category 'API Credential' \
  "SCANOPY_POSTGRES_USER[text]=scanopy" \
  "SCANOPY_POSTGRES_PASSWORD[password]=$(openssl rand -hex 24)" \
  "SCANOPY_DAEMON_API_KEY[password]=$(openssl rand -hex 32)" \
  "SCANOPY_NETWORK_ID[text]=$(uuidgen | tr '[:upper:]' '[:lower:]')" \
  "SNMP_COMMUNITY[password]=<LUKE-SUPPLIES-THIS>"
```

- [ ] **P2: Verify the `cloudnative-pg` item exposes both superuser fields.**

```bash
op item get cloudnative-pg --vault Talos --fields POSTGRES_SUPER_USER,POSTGRES_SUPER_PASS
```

Expected: both fields return non-empty. If `POSTGRES_SUPER_USER` is absent, hardcode `INIT_POSTGRES_SUPER_USER: postgres` in Task 3 instead of templating it.

---

## Task 1: Scaffold directory and Flux Kustomization (`ks.yaml`)

**Files:**

- Create: `kubernetes/apps/network/scanopy/ks.yaml`

- [ ] **Step 1: Create `ks.yaml`**

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
    name: &app scanopy
    namespace: &namespace network
spec:
    commonMetadata:
        labels:
            app.kubernetes.io/name: *app
    components:
        - ../../../../components/volsync
        - ../../../../components/gatus/guarded
    dependsOn:
        - name: cloudnative-pg-cluster
          namespace: database
    interval: 1h
    path: ./kubernetes/apps/network/scanopy/app
    postBuild:
        substitute:
            APP: *app
            GATUS_SUBDOMAIN: scanopy
            VOLSYNC_CAPACITY: 5Gi
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

- [ ] **Step 2: Format**

Run: `yamlfmt kubernetes/apps/network/scanopy/ks.yaml && prettier --write kubernetes/apps/network/scanopy/ks.yaml`
Expected: exits 0, no diff on re-run.

---

## Task 2: OCIRepository + app `kustomization.yaml`

**Files:**

- Create: `kubernetes/apps/network/scanopy/app/kustomization.yaml`
- Create: `kubernetes/apps/network/scanopy/app/ocirepository.yaml`

- [ ] **Step 1: Create `app/ocirepository.yaml`**

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
    name: scanopy
spec:
    interval: 1h
    layerSelector:
        mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
        operation: copy
    ref:
        tag: 5.0.1
    url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

- [ ] **Step 2: Create `app/kustomization.yaml`**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
    - ocirepository.yaml
    - externalsecret.yaml
    - externalsecret-snmp.yaml
    - helmrelease.yaml
    - daemon-helmrelease.yaml
```

- [ ] **Step 3: Format**

Run: `yamlfmt kubernetes/apps/network/scanopy/app/*.yaml && prettier --write kubernetes/apps/network/scanopy/app/kustomization.yaml`
Expected: exits 0.

> Note: `kustomization.yaml` references files created in Tasks 3–5. `kustomize build` will fail until those exist — that is expected; first full validation is Task 7.

---

## Task 3: ExternalSecrets (DB + SNMP)

**Files:**

- Create: `kubernetes/apps/network/scanopy/app/externalsecret.yaml`
- Create: `kubernetes/apps/network/scanopy/app/externalsecret-snmp.yaml`

- [ ] **Step 1: Create `app/externalsecret.yaml`**

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
    name: scanopy
spec:
    secretStoreRef:
        kind: ClusterSecretStore
        name: onepassword-connect
    target:
        name: scanopy-secret
        creationPolicy: Owner
        template:
            engineVersion: v2
            data:
                # ---- postgres-init initContainer ----
                INIT_POSTGRES_DBNAME: scanopy
                INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
                INIT_POSTGRES_USER: "{{ .SCANOPY_POSTGRES_USER }}"
                INIT_POSTGRES_PASS: "{{ .SCANOPY_POSTGRES_PASSWORD }}"
                INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
                INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
                # ---- scanopy server/daemon ----
                # TLS enforced; CNPG presents a valid cert-manager cert (postgres18-tls).
                SCANOPY_DATABASE_URL: postgresql://{{ .SCANOPY_POSTGRES_USER }}:{{ .SCANOPY_POSTGRES_PASSWORD }}@postgres18-rw.database.svc.cluster.local:5432/scanopy?sslmode=require
                SCANOPY_DAEMON_API_KEY: "{{ .SCANOPY_DAEMON_API_KEY }}"
                SCANOPY_NETWORK_ID: "{{ .SCANOPY_NETWORK_ID }}"
    dataFrom:
        - extract:
              key: cloudnative-pg
        - extract:
              key: scanopy
```

- [ ] **Step 2: Create `app/externalsecret-snmp.yaml`**

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
    name: scanopy-snmp
spec:
    secretStoreRef:
        kind: ClusterSecretStore
        name: onepassword-connect
    target:
        name: scanopy-snmp-secret
        creationPolicy: Owner
        template:
            engineVersion: v2
            data:
                community: "{{ .SNMP_COMMUNITY }}"
    dataFrom:
        - extract:
              key: scanopy
```

- [ ] **Step 3: Format**

Run: `yamlfmt kubernetes/apps/network/scanopy/app/externalsecret*.yaml`
Expected: exits 0.

---

## Task 4: Server HelmRelease (`helmrelease.yaml`)

**Files:**

- Create: `kubernetes/apps/network/scanopy/app/helmrelease.yaml`

- [ ] **Step 1: Create `app/helmrelease.yaml`**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
    name: &app scanopy
spec:
    interval: 1h
    chartRef:
        kind: OCIRepository
        name: scanopy
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
            scanopy:
                annotations:
                    reloader.stakater.com/auto: "true"
                initContainers:
                    init-db:
                        image:
                            repository: ghcr.io/home-operations/postgres-init
                            tag: 18@sha256:5086f94abc783f1147d7c2a32c01db00ab594820026e4f6a82ac2af3dbde7fc7
                        envFrom: &envFrom
                            - secretRef:
                                  name: scanopy-secret
                containers:
                    app:
                        image:
                            repository: ghcr.io/scanopy/scanopy/server
                            tag: v0.16.2@sha256:e3c55cd639b9643c01e21c1fcc5470c6524ea5c3ac316768bf028737c656bbe6
                        env:
                            TZ: ${TIMEZONE}
                            SCANOPY_LOG_LEVEL: info
                            SCANOPY_PUBLIC_URL: https://scanopy.${SECRET_DOMAIN}
                            SCANOPY_WEB_EXTERNAL_PATH: /app/static
                            SCANOPY_USE_SECURE_SESSION_COOKIES: "true"
                            SCANOPY_SMTP_RELAY: smtp-relay.infrastructure.svc.cluster.local:25
                            SCANOPY_SMTP_EMAIL: scanopy@${SECRET_DOMAIN}
                        envFrom: *envFrom
                        probes:
                            liveness: &probe
                                enabled: true
                                custom: true
                                spec:
                                    httpGet:
                                        path: /api/health
                                        port: &port 60072
                                    initialDelaySeconds: 15
                                    periodSeconds: 10
                                    timeoutSeconds: 5
                                    failureThreshold: 3
                            readiness: *probe
                        resources:
                            requests:
                                cpu: 50m
                                memory: 256Mi
                            limits:
                                memory: 512Mi
        defaultPodOptions:
            dnsConfig:
                options:
                    - { name: ndots, value: "1" }
        service:
            app:
                controller: scanopy
                ports:
                    http:
                        port: *port
        route:
            app:
                hostnames:
                    - "{{ .Release.Name }}.${SECRET_DOMAIN}"
                    - "{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}"
                parentRefs:
                    - name: envoy-internal
                      namespace: network
        persistence:
            data:
                existingClaim: scanopy
                globalMounts:
                    - path: /data
```

> The server runs with no restrictive `securityContext` (it writes `/data`, reads `/app/static`). This matches the reference impl. Harden to `runAsNonRoot`/`readOnlyRootFilesystem` only after confirming the container stays up (apply-time Task 9).

- [ ] **Step 2: Format**

Run: `yamlfmt kubernetes/apps/network/scanopy/app/helmrelease.yaml`
Expected: exits 0.

---

## Task 5: Daemon HelmRelease (`daemon-helmrelease.yaml`)

**Files:**

- Create: `kubernetes/apps/network/scanopy/app/daemon-helmrelease.yaml`

- [ ] **Step 1: Create `app/daemon-helmrelease.yaml`**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
    name: &app scanopy-daemon
spec:
    interval: 1h
    chartRef:
        kind: OCIRepository
        name: scanopy
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
            scanopy-daemon:
                type: daemonset
                annotations:
                    reloader.stakater.com/auto: "true"
                containers:
                    app:
                        image:
                            repository: ghcr.io/scanopy/scanopy/daemon
                            tag: v0.16.2@sha256:98cdba02b2b127462cba87c10dacdc5671354e2dd43a124b72f8cfd2dfb4b911
                        env:
                            TZ: ${TIMEZONE}
                            SCANOPY_LOG_LEVEL: info
                            SCANOPY_SERVER_URL: http://scanopy.network.svc.cluster.local:60072
                            SCANOPY_ENABLE_LOCAL_DOCKER_SOCKET: "false"
                            SCANOPY_NAME:
                                valueFrom:
                                    fieldRef:
                                        fieldPath: spec.nodeName
                        envFrom:
                            - secretRef:
                                  name: scanopy-secret
                        probes:
                            liveness: &probe
                                enabled: true
                                custom: true
                                spec:
                                    httpGet:
                                        path: /api/health
                                        port: &port 60073
                                    initialDelaySeconds: 15
                                    periodSeconds: 10
                                    timeoutSeconds: 5
                                    failureThreshold: 3
                            readiness: *probe
                        securityContext:
                            privileged: true
                        resources:
                            requests:
                                cpu: 50m
                                memory: 128Mi
                            limits:
                                memory: 512Mi
        defaultPodOptions:
            hostNetwork: true
            dnsPolicy: ClusterFirstWithHostNet
        persistence:
            config:
                type: hostPath
                hostPath: /var/lib/scanopy-daemon
                globalMounts:
                    - path: /root/.config/scanopy/daemon
            snmp-community:
                type: secret
                name: scanopy-snmp-secret
                globalMounts:
                    - path: /run/secrets/snmp-community
                      subPath: community
                      readOnly: true
```

- [ ] **Step 2: Format**

Run: `yamlfmt kubernetes/apps/network/scanopy/app/daemon-helmrelease.yaml`
Expected: exits 0.

---

## Task 6: Register the app in the network kustomization

**Files:**

- Modify: `kubernetes/apps/network/kustomization.yaml`

- [ ] **Step 1: Insert the resource line (alphabetical slot)**

Change the `resources:` list so the scanopy entry sits between `opnsense-dns` and `tailscale-operator`:

```yaml
resources:
    - ./namespace.yaml
    - ./netpol.yaml
    - ./certificates/ks.yaml
    - ./cloudflare-dns/ks.yaml
    - ./cloudflare-tunnel/ks.yaml
    - ./envoy-gateway/ks.yaml
    - ./error-pages/ks.yaml
    - ./opnsense-dns/ks.yaml
    - ./scanopy/ks.yaml
    - ./tailscale-operator/ks.yaml
```

- [ ] **Step 2: Format**

Run: `yamlfmt kubernetes/apps/network/kustomization.yaml && prettier --write kubernetes/apps/network/kustomization.yaml`
Expected: exits 0.

---

## Task 7: Validate the full app render

**Files:** none (validation only)

- [ ] **Step 1: Raw kustomize build sanity**

Run: `kustomize build kubernetes/apps/network/scanopy/app`
Expected: renders all 5 resources (1 OCIRepository, 2 ExternalSecrets, 2 HelmReleases) with no error. `${...}` placeholders remain unsubstituted at this layer — that is expected (Flux substitutes at apply).

- [ ] **Step 2: flux-local / flate build of the Kustomization**

Run: `flate test all --path kubernetes/flux/cluster --allow-missing-secrets`
Expected: passes; the `scanopy` Kustomization builds and its HelmReleases template without error. (This is the trusted K8s validator for this repo — kubeconform-on-raw-source is disabled here.)

- [ ] **Step 3: Secret-key coverage grep**

Confirm every secret key the manifests reference is actually produced by an ExternalSecret template.

Run:

```bash
grep -rhoE 'secretRef:\s*$|name: scanopy(-snmp)?-secret' kubernetes/apps/network/scanopy/app
grep -nE 'INIT_POSTGRES|SCANOPY_|community' kubernetes/apps/network/scanopy/app/externalsecret*.yaml
```

Expected: `scanopy-secret` carries `INIT_POSTGRES_*`, `SCANOPY_DATABASE_URL`, `SCANOPY_DAEMON_API_KEY`, `SCANOPY_NETWORK_ID`; `scanopy-snmp-secret` carries `community`. No HelmRelease references a key absent from these (e.g. nothing reads a key only present in 1Password but not templated).

- [ ] **Step 4: Lint**

Run: `just lint`
Expected: super-linter mirror passes (or only pre-existing unrelated findings).

---

## Task 8: Commit and open PR

**Files:** none (git only)

- [ ] **Step 1: Stage and commit**

```bash
git add kubernetes/apps/network/scanopy kubernetes/apps/network/kustomization.yaml
git commit -m "feat(scanopy): deploy network-documentation tool

Server+UI Deployment on the shared CNPG cluster (sslmode=require) plus a
privileged hostNetwork DaemonSet scanner. Exposed on envoy-internal,
volsync-backed /data, SNMP community + DB creds via 1Password
ExternalSecrets. Docker-socket scan source disabled (Talos/containerd)."
```

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin docs/scanopy-design
gh pr create --fill --base main
```

Expected: PR created. CI (flate test, super-linter, claude/renovate-review N/A) runs green.

> The branch is currently `docs/scanopy-design`. If a clean `feat/scanopy` branch is preferred, branch off `main` first and cherry-pick — but reusing the existing branch (which already holds the spec) is fine.

---

## Task 9: Apply-time verification (after merge / reconcile)

**Files:** none (live-cluster checks). Run only once Flux has reconciled the new Kustomization.

- [ ] **Step 1: Kustomization + HelmReleases healthy**

```bash
flux -n network get kustomization scanopy
flux -n network get helmrelease scanopy scanopy-daemon
```

Expected: all `Ready=True`. If a HR thrashes on the 5m timeout, see the `project_helmrelease_5m_timeout_thrash` runbook (suspend → fix → resume).

- [ ] **Step 2: ExternalSecrets synced**

```bash
kubectl -n network get externalsecret scanopy scanopy-snmp
kubectl -n network get secret scanopy-secret scanopy-snmp-secret
```

Expected: both ExternalSecrets `SecretSynced=True`; both target Secrets exist.

- [ ] **Step 3: DB TLS connect succeeded**

```bash
kubectl -n network logs deploy/scanopy -c init-db
kubectl -n network logs deploy/scanopy -c app | grep -iE 'tls|ssl|database|connect'
```

Expected: init-db created DB/user; server connected. If a TLS handshake error appears, mount `postgres18-tls`'s `ca.crt` and switch the URL to `verify-full` with `sslrootcert` — do **not** drop to `sslmode=disable`.

- [ ] **Step 4: Daemons registered (one per node)**

```bash
kubectl -n network get pods -l app.kubernetes.io/name=scanopy -o wide
kubectl -n network logs ds/scanopy-daemon | grep -iE 'register|network|server'
```

Expected: one daemon pod per node, each registered against the server using the shared `SCANOPY_NETWORK_ID`/`SCANOPY_DAEMON_API_KEY`. If registration needs a "network" object first, create the admin + network in the UI, then confirm.

- [ ] **Step 5: hostNetwork port + UI reachable**

```bash
kubectl -n network get pods -l app.kubernetes.io/name=scanopy -o jsonpath='{range .items[*]}{.spec.hostNetwork}{"\t"}{.status.hostIP}{"\n"}{end}'
```

Then browse `https://scanopy.${SECRET_DOMAIN}` (internal). Expected: UI loads; first-run creates the admin (built-in password login). Confirm nothing else on the nodes already binds `60073`.

- [ ] **Step 6: SNMP + SMTP**

- In the Scanopy UI, create an SNMP credential pointing at the mounted file `/run/secrets/snmp-community`; confirm a switch/AP/router is discovered.
- Trigger a test email (e.g. a user invite); confirm it relays via `smtp-relay.infrastructure`.

---

## Self-review notes

- **Spec coverage:** every §2 locked decision maps to a task — daemon networking (T5), database+init (T3/T4), DB TLS (T3, corrected), auth/SMTP/SNMP (T3–T5, T9.6), namespace/exposure (T1/T4/T6), health (T1 gatus), backups (T1 volsync). §5 secrets → Prereqs. §6 digests → resolved inline. §7 risks → T9. §8 validation → T7. §9 public-repo hygiene → SNMP via ExternalSecret + neutral mount path, `${SECRET_*}` placeholders only.
- **No placeholders:** every manifest is complete with real digests; the only intentional human input is `SNMP_COMMUNITY` (P1) and the PR branch choice (T8).
- **Type/name consistency:** `scanopy` (OCIRepository / server HR / Service / PVC / ExternalSecret target `scanopy-secret`) and `scanopy-daemon` (daemon HR) used consistently; both HRs reference `chartRef.name: scanopy`; daemon `envFrom` + server `envFrom` both read `scanopy-secret`; SNMP secret `scanopy-snmp-secret` produced by `externalsecret-snmp.yaml` and mounted in T5.

```

```
