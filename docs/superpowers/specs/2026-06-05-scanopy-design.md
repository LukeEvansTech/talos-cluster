# Scanopy — Deployment Design Spec

**Date:** 2026-06-05
**Status:** Approved design, ready for implementation plan
**Target:** `kubernetes/apps/network/scanopy/`
**Upstream:** https://github.com/scanopy/scanopy (AGPL-3.0)
**Reference impl:** https://github.com/zebernst/homelab `kubernetes/apps/observability/scanopy/app` (Talos/Flux, hostNetwork DaemonSet)

---

## 1. What scanopy is

Network-documentation tool that auto-generates L2/L3/workload/application diagrams by
continuously scanning infrastructure. Three parts:

- **server** — Rust API + bundled Svelte UI. Image `ghcr.io/scanopy/scanopy/server`, port **60072**.
- **daemon** — Rust scanner (SNMP / LLDP / ARP). Image `ghcr.io/scanopy/scanopy/daemon`, port **60073**.
- **postgres** — upstream compose uses `postgres:17-alpine`; **we use the shared CNPG cluster instead**.

Current upstream release: **v0.16.2** (2026-04-28) — also the version the reference repo pins.

The upstream Docker-socket scan source (`/var/run/docker.sock`) is **dropped** — Talos runs
containerd, there is no docker socket. Disabled explicitly via `SCANOPY_ENABLE_LOCAL_DOCKER_SOCKET=false`.

---

## 2. Decisions (locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Daemon networking | **Privileged `hostNetwork` DaemonSet** | Proven reference path; one per-node scanner, named per node. L2 reach to the nodes' primary LAN + L3/SNMP reach to anything routable. Multus rejected: only existing NAD (`iot`) is a macvlan on the same primary NIC, so it adds nothing over hostNetwork unless we author VLAN-tagged NADs. |
| Database | **Shared CNPG `postgres18-rw.database.svc`** | Cluster standard (metabase, netbox). `init-db` initContainer provisions DB + user. |
| DB TLS | **No `sslmode` param (TLS opportunistic, unverified)** | CNPG presents `postgres18-tls` AND accepts plaintext. Scanopy's Rust client defaults to `sslmode=prefer` → uses TLS without CA verification, so CNPG's empty-issuer-DN cert (which broke Metabase's Java verifier — see `project_cnpg_jvm_jdbc_ssl`) is a non-issue. Fallback only if handshake fails at apply: append `?sslmode=disable`. |
| Auth | **Built-in password login** (no OIDC) | First-run creates admin in UI. No `oidc.toml`, no pocket-id client. Can be added later. |
| SMTP | **In** — `smtp-relay.infrastructure.svc.cluster.local:25` | Canonical internal relay (used by pocket-id/epicgames), unauthenticated. |
| SNMP | **In** — community via ExternalSecret, mounted read-only at neutral path | Community string lives only in 1Password (repo is PUBLIC). Mount path `/run/secrets/snmp-community` — no vendor name in git. SNMP credential then configured in scanopy UI pointing at the file. |
| Namespace | **`network`** | Network-documentation/discovery tool; sits with envoy/opnsense-dns/cloudflare/tailscale. |
| Exposure | **envoy-internal only**, dual hostname | `scanopy.${SECRET_DOMAIN}` + `scanopy.${SECRET_INTERNAL_DOMAIN}`. Internal-app standard. |
| Health | **gatus/guarded** component | Standard. No PodMonitor — scanopy exposes no Prometheus metrics. |
| Backups | **volsync** component, `VOLSYNC_CAPACITY: 5Gi` | Server `/data` PVC. |

### Out of scope for v1 (clean follow-ups)
- OIDC / pocket-id SSO (`oidc.toml`, `SCANOPY_DISABLE_PASSWORD_LOGIN`).
- PodMonitor / metrics (none exposed).

---

## 3. File layout

```
kubernetes/apps/network/scanopy/
├── ks.yaml                     # Flux Kustomization (network ns)
└── app/
    ├── kustomization.yaml      # lists the 5 resources below
    ├── ocirepository.yaml      # app-template 5.0.1, name: scanopy (shared by both HRs)
    ├── externalsecret.yaml     # scanopy-secret  (DB + daemon pairing + SMTP-less env)
    ├── externalsecret-snmp.yaml# scanopy-snmp-secret (community)
    ├── helmrelease.yaml        # server + UI (Deployment)
    └── daemon-helmrelease.yaml # scanner (privileged hostNetwork DaemonSet)
```

Plus register `./scanopy/ks.yaml` in `kubernetes/apps/network/kustomization.yaml`
(alphabetical — **between `./opnsense-dns/ks.yaml` and `./tailscale-operator/ks.yaml`**).

Convention notes verified against the repo:
- Per-app `OCIRepository` named after the app, `oci://ghcr.io/bjw-s-labs/helm/app-template` tag `5.0.1`,
  with the `layerSelector` block (see `home/shellyctl/app/ocirepository.yaml`). **One** OCIRepository,
  referenced by **both** HelmReleases via `chartRef: { kind: OCIRepository, name: scanopy }`.
- `volsync` component goes in `ks.yaml` `spec.components`, NOT in `app/kustomization.yaml` (double-apply).
- Privileged is allowed: cluster sets **no** restricted PodSecurity enforcement (kube-system already
  runs privileged `fstrim` / `generic-device-plugin`). `network/namespace.yaml` has no PSA labels.

---

## 4. Manifests (draft — verify image digests before commit)

### 4.1 `ks.yaml`
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
    - name: onepassword
      namespace: external-secrets
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
> **Verify** the exact `dependsOn` names against a recent `network`/`database` app — the CNPG
> dependency is the Flux Kustomization that builds the cluster (reference uses
> `cloudnative-pg-cluster`/`database`; metabase uses `cloudnative-pg-cluster`/`database`). The
> onepassword dependency name in this repo is `onepassword` in `external-secrets` (confirm).

### 4.2 `app/kustomization.yaml`
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

### 4.3 `app/ocirepository.yaml`
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

### 4.4 `app/externalsecret.yaml`
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
    name: onepassword-connect          # confirm store name (metabase uses onepassword-connect)
  target:
    name: scanopy-secret
    creationPolicy: Owner
    template:
      engineVersion: v2
      data:
        # ---- init-db (postgres-init initContainer) ----
        INIT_POSTGRES_DBNAME: scanopy
        INIT_POSTGRES_HOST: postgres18-rw.database.svc.cluster.local
        INIT_POSTGRES_USER: "{{ .SCANOPY_POSTGRES_USER }}"
        INIT_POSTGRES_PASS: "{{ .SCANOPY_POSTGRES_PASSWORD }}"
        INIT_POSTGRES_SUPER_USER: "{{ .POSTGRES_SUPER_USER }}"
        INIT_POSTGRES_SUPER_PASS: "{{ .POSTGRES_SUPER_PASS }}"
        # ---- scanopy server/daemon ----
        # TLS opportunistic/unverified (Rust default sslmode=prefer); no sslmode param.
        SCANOPY_DATABASE_URL: postgresql://{{ .SCANOPY_POSTGRES_USER }}:{{ .SCANOPY_POSTGRES_PASSWORD }}@postgres18-rw.database.svc.cluster.local:5432/scanopy
        SCANOPY_DAEMON_API_KEY: "{{ .SCANOPY_DAEMON_API_KEY }}"
        SCANOPY_NETWORK_ID: "{{ .SCANOPY_NETWORK_ID }}"
  dataFrom:
    - extract:
        key: cloudnative-pg            # provides POSTGRES_SUPER_USER / POSTGRES_SUPER_PASS
    - extract:
        key: scanopy                   # provides the SCANOPY_* values
```
> Confirm whether the `cloudnative-pg` 1Password item exposes `POSTGRES_SUPER_USER` (metabase
> references both `POSTGRES_SUPER_USER` and `POSTGRES_SUPER_PASS`). If only `POSTGRES_SUPER_PASS`
> exists, hardcode `INIT_POSTGRES_SUPER_USER: postgres`.

### 4.5 `app/externalsecret-snmp.yaml`
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

### 4.6 `app/helmrelease.yaml` (server)
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
              tag: 18.4.0            # match digest used elsewhere (metabase pins 18.4.0@sha256:...)
            envFrom: &envFrom
              - secretRef:
                  name: scanopy-secret
        containers:
          app:
            image:
              repository: ghcr.io/scanopy/scanopy/server
              tag: v0.16.2          # @sha256:<RESOLVE DIGEST>
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
        annotations:
          gatus.home-operations.com/endpoint: |
            path: /api/health
        hostnames:
          - "{{ .Release.Name }}.${SECRET_DOMAIN}"
          - "{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}"
        parentRefs:
          - name: envoy-internal
            namespace: network
    persistence:
      data:
        existingClaim: scanopy        # PVC created by the volsync component
        globalMounts:
          - path: /data
```
> The server container may need to run as root (writes `/data`, reads `/app/static`). Reference
> set no pod `securityContext` for the server — start permissive, harden later only if it stays up.

### 4.7 `app/daemon-helmrelease.yaml` (scanner)
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
              tag: v0.16.2          # @sha256:<RESOLVE DIGEST>
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

---

## 5. Secrets — 1Password `Talos` vault

Create via `op` CLI (never paste into UI — see `feedback_op_cli_for_secrets`). New item **`scanopy`**:

| Field | Value | How |
|-------|-------|-----|
| `SCANOPY_POSTGRES_USER` | `scanopy` | literal |
| `SCANOPY_POSTGRES_PASSWORD` | random | `openssl rand -hex 24` |
| `SCANOPY_DAEMON_API_KEY` | random | `openssl rand -hex 32` |
| `SCANOPY_NETWORK_ID` | uuid | `uuidgen` (lowercase) |
| `SNMP_COMMUNITY` | **provided by Luke** | his network's SNMP community string — the ONE value I can't generate |

Reuses existing **`cloudnative-pg`** item for `POSTGRES_SUPER_USER` / `POSTGRES_SUPER_PASS`.

> Items must live in the `Talos` vault (the ClusterSecretStore only reads `Talos`). If a source
> value lives in `Home Operations`, copy across with `op read`.

---

## 6. Image digests to resolve before commit

```
ghcr.io/scanopy/scanopy/server:v0.16.2  -> @sha256:...
ghcr.io/scanopy/scanopy/daemon:v0.16.2  -> @sha256:...
ghcr.io/home-operations/postgres-init   -> reuse the 18.4.0@sha256 pin metabase uses
```
Resolve with e.g. `docker buildx imagetools inspect <ref>` or `crane digest <ref>` (neither crane
nor skopeo is on this box yet — `mise`/`brew` install, or pull digests from another machine).
Renovate manages bumps afterward.

---

## 7. Risks / things to verify at apply-time

1. **DB TLS** — expect TLS-unverified connect to succeed. If logs show a TLS handshake error,
   append `?sslmode=disable` to `SCANOPY_DATABASE_URL` (CNPG accepts plaintext — proven by metabase).
2. **First-run pairing** — with password login, create the admin in the UI, then confirm each
   per-node daemon registers against the server using the shared `SCANOPY_NETWORK_ID` /
   `SCANOPY_DAEMON_API_KEY`. If the daemon won't register until a "network" exists, create it in-UI.
3. **hostNetwork port 60073** — every node binds 60073 on its host IP; confirm nothing else uses it.
4. **hostPath `/var/lib/scanopy-daemon`** — confirm it persists across reboots on Talos (it should;
   `/var/lib` is writable + persistent).
5. **SNMP** — after deploy, in scanopy UI create an SNMP credential referencing the mounted file
   `/run/secrets/snmp-community`; verify a switch/AP/router is discovered via SNMP.
6. **SMTP** — trigger a test email (e.g. user invite) and confirm it relays via
   `smtp-relay.infrastructure`.

---

## 8. Validation before commit

- `flate test all --path kubernetes/flux/cluster --allow-missing-secrets`
- `just lint` (super-linter mirror)
- Grep the rendered server/daemon manifests for every `secretKeyRef`/projected `items[].key` to
  confirm `scanopy-secret` / `scanopy-snmp-secret` carry every key referenced
  (see CLAUDE.md `existingSecret` runbook).

---

## 9. Public-repo hygiene reminders

Repo is PUBLIC. No LAN IPs, no `.lan`/`.internal` hostnames, no device/vendor names in git.
- SNMP community → ExternalSecret only; mount path is the neutral `/run/secrets/snmp-community`.
- `${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}` placeholders are fine (Flux substitutes at apply).
```
