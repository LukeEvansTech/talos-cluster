# Reclaimerr Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Reclaimerr (Jellyfin/Plex disk-space reclamation tool) to the `media` namespace via Flux GitOps, serving an internal-only web UI.

**Architecture:** Standard bjw-s app-template deployment (mirroring maintainerr/pulsarr). Flux Kustomization (`ks.yaml`) uses `gatus/guarded`, `homepage`, and `volsync` components. The HelmRelease runs one pod with a VolSync-backed PVC at `/app/data` (2Gi), serving port 8000 through Envoy Gateway on the internal listener. `JWT_SECRET` and `ENCRYPTION_KEY` auto-generate into the PVC on first launch — no ExternalSecret.

**Tech Stack:** Flux CD v2, bjw-s `app-template` OCI Helm chart v4.6.2, Kustomize, Envoy Gateway (Gateway API), VolSync, Rook-Ceph.

**Spec:** `docs/superpowers/specs/2026-04-17-reclaimerr-deployment-design.md`

---

## File Structure

**Created:**
- `kubernetes/apps/media/reclaimerr/ks.yaml` — Flux Kustomization entry point
- `kubernetes/apps/media/reclaimerr/app/kustomization.yaml` — Kustomize resource list
- `kubernetes/apps/media/reclaimerr/app/ocirepository.yaml` — OCI chart source (bjw-s app-template)
- `kubernetes/apps/media/reclaimerr/app/helmrelease.yaml` — HelmRelease with image, env, service, route, persistence

**Modified:**
- `kubernetes/apps/media/kustomization.yaml` — register `./reclaimerr/ks.yaml` alphabetically

Each file has one responsibility, following established conventions. No deviation from pattern.

---

## Task 1: Create Flux Kustomization (`ks.yaml`)

**Files:**
- Create: `kubernetes/apps/media/reclaimerr/ks.yaml`

- [ ] **Step 1: Write `ks.yaml`**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app reclaimerr
  namespace: &namespace media
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  components:
    - ../../../../components/gatus/guarded
    - ../../../../components/homepage
    - ../../../../components/volsync
  dependsOn:
    - name: rook-ceph-cluster
      namespace: rook-ceph
  interval: 1h
  path: ./kubernetes/apps/media/reclaimerr/app
  postBuild:
    substitute:
      APP: *app
      VOLSYNC_CAPACITY: 2Gi
      HOMEPAGE_NAME: Reclaimerr
      HOMEPAGE_GROUP: Media
      HOMEPAGE_ICON: mdi-broom
      HOMEPAGE_DESCRIPTION: Disk space reclamation
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

- [ ] **Step 2: Commit**

```bash
git add kubernetes/apps/media/reclaimerr/ks.yaml
git commit -m "feat(reclaimerr): add Flux Kustomization"
```

---

## Task 2: Create OCI chart source (`ocirepository.yaml`)

**Files:**
- Create: `kubernetes/apps/media/reclaimerr/app/ocirepository.yaml`

- [ ] **Step 1: Write `ocirepository.yaml`**

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: reclaimerr
spec:
  interval: 1h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 4.6.2
  url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

- [ ] **Step 2: Commit** (deferred — bundled with helmrelease in task 4)

No commit yet. File will be included in the commit for task 4.

---

## Task 3: Create Kustomize resource list (`app/kustomization.yaml`)

**Files:**
- Create: `kubernetes/apps/media/reclaimerr/app/kustomization.yaml`

- [ ] **Step 1: Write `app/kustomization.yaml`**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./ocirepository.yaml
  - ./helmrelease.yaml
```

- [ ] **Step 2: Commit** (deferred — bundled with helmrelease in task 4)

No commit yet.

---

## Task 4: Create HelmRelease (`helmrelease.yaml`)

**Files:**
- Create: `kubernetes/apps/media/reclaimerr/app/helmrelease.yaml`

- [ ] **Step 1: Write `helmrelease.yaml`**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: reclaimerr
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: reclaimerr
  values:
    controllers:
      reclaimerr:
        containers:
          app:
            image:
              repository: ghcr.io/jessielw/reclaimerr
              tag: 0.1.0-beta7@sha256:b41300380197333584c09effa16a4b10caa31806ad145c0c46a04f7124ed33a4
            env:
              TZ: ${TIMEZONE}
              API_HOST: 0.0.0.0
              API_PORT: &port 8000
              COOKIE_SECURE: "true"
              CORS_ORIGINS: https://reclaimerr.${SECRET_DOMAIN}
              LOG_LEVEL: INFO
            probes:
              liveness: &probes
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/info/health
                    port: *port
                  initialDelaySeconds: 0
                  periodSeconds: 10
                  timeoutSeconds: 3
                  failureThreshold: 3
              readiness: *probes
              startup:
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /api/info/health
                    port: *port
                  failureThreshold: 30
                  periodSeconds: 5
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: true
              capabilities: {drop: ["ALL"]}
            resources:
              requests:
                cpu: 10m
              limits:
                memory: 512Mi
    defaultPodOptions:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
    service:
      app:
        controller: reclaimerr
        ports:
          http:
            port: *port
    route:
      app:
        annotations:
          gatus.home-operations.com/endpoint: |-
            group: internal
            url: http://reclaimerr.media.svc.cluster.local:8000/api/info/health
            conditions: ["[STATUS] == 200"]
        hostnames:
          - "{{ .Release.Name }}.${SECRET_DOMAIN}"
          - "{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}"
        parentRefs:
          - name: envoy-internal
            namespace: network
    persistence:
      config:
        existingClaim: "{{ .Release.Name }}"
        globalMounts:
          - path: /app/data
      tmp:
        type: emptyDir
        globalMounts:
          - path: /tmp
```

- [ ] **Step 2: Commit the app directory**

```bash
git add kubernetes/apps/media/reclaimerr/app/
git commit -m "feat(reclaimerr): add HelmRelease, OCIRepository, kustomization"
```

---

## Task 5: Register in namespace kustomization

**Files:**
- Modify: `kubernetes/apps/media/kustomization.yaml` (insert between `./radarr/ks.yaml` and `./recommendarr/ks.yaml`)

- [ ] **Step 1: Edit to add reclaimerr entry**

Insert the line `  - ./reclaimerr/ks.yaml` between the radarr entry (line 38 `  - ./radarr/ks.yaml`) and the `  - ./recommendarr/ks.yaml` entry (currently on line 41 after commented lines).

Exact edit — replace:

```yaml
  - ./radarr/ks.yaml
  # - ./radarr4k/ks.yaml  # Removed — consolidated into radarr (GPU transcoding)
  # - ./radarranime/ks.yaml  # Removed — consolidated into radarr (anime handled via root folders + profiles)
  - ./recommendarr/ks.yaml
```

with:

```yaml
  - ./radarr/ks.yaml
  # - ./radarr4k/ks.yaml  # Removed — consolidated into radarr (GPU transcoding)
  # - ./radarranime/ks.yaml  # Removed — consolidated into radarr (anime handled via root folders + profiles)
  - ./reclaimerr/ks.yaml
  - ./recommendarr/ks.yaml
```

- [ ] **Step 2: Commit**

```bash
git add kubernetes/apps/media/kustomization.yaml
git commit -m "feat(reclaimerr): register in media namespace"
```

---

## Task 6: Validate with flux-local

**Files:** none modified.

- [ ] **Step 1: Run flux-local test**

Run: `flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster --verbose`

Expected: all tests pass. If tests fail specifically in `media/reclaimerr`, inspect error, fix, and re-run. Common failures:
- YAML indentation mismatch in helmrelease.yaml → re-check alignment with maintainerr pattern.
- Wrong component path → all four `../` segments needed from `kubernetes/apps/media/reclaimerr/ks.yaml` to reach `components/`.
- Missing `VOLSYNC_CAPACITY` → required by the `volsync` component.

- [ ] **Step 2: Run flux-local diff (sanity check)**

Run: `flux-local diff --all-namespaces --enable-helm --path kubernetes/flux/cluster --branch-orig main`

Expected: diff shows the new resources for `reclaimerr` (HelmRelease, OCIRepository, Gatus endpoint, Homepage entry, PVC, ReplicationSource/Destination from VolSync).

- [ ] **Step 3: Fix anything flagged by lefthook pre-commit**

Run: `lefthook run pre-commit --all-files` (or rely on commit hooks firing automatically on the commits above).

Expected: yamlfmt, prettier, shellcheck, actionlint all pass on the new files. If yamlfmt reformats any file, re-stage and amend or create a fixup commit.

---

## Task 7: Push and verify in cluster

**Files:** none modified.

- [ ] **Step 1: Push the branch and open PR**

Push the current branch, open a PR, wait for `flux-local` and `security-scans` CI to go green.

- [ ] **Step 2: After merge, force Flux reconcile**

```bash
export KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig
flux reconcile source git flux-system
flux reconcile kustomization cluster-apps -n flux-system
flux reconcile kustomization reclaimerr -n media
flux reconcile helmrelease reclaimerr -n media
```

Expected: kustomization `reclaimerr` reaches `Ready=True`, HelmRelease `reclaimerr` reaches `Ready=True`.

- [ ] **Step 3: Verify pod and PVC**

```bash
kubectl -n media get pvc reclaimerr
kubectl -n media get pods -l app.kubernetes.io/name=reclaimerr
kubectl -n media logs -l app.kubernetes.io/name=reclaimerr --tail=50
```

Expected: PVC `Bound`, pod `Running` and `Ready=1/1`, logs show app started and listening on :8000 (JWT/encryption keys auto-generated into `/app/data` on first launch).

- [ ] **Step 4: Verify HTTPRoute and health endpoint**

```bash
kubectl -n media get httproute reclaimerr -o yaml | grep -A2 parents:
curl -sk "https://reclaimerr.${SECRET_INTERNAL_DOMAIN}/api/info/health"
```

Expected: HTTPRoute accepted by `envoy-internal`. Health endpoint returns a 200 JSON body. Gatus `reclaimerr-internal` endpoint shows healthy within one check cycle.

- [ ] **Step 5: Confirm Homepage tile**

Visit the Homepage dashboard internal URL and confirm `Reclaimerr` appears under the `Media` group with the `mdi-broom` icon and links to the internal hostname.

---

## Rollback

If the deployment misbehaves:

```bash
flux suspend helmrelease reclaimerr -n media
kubectl -n media delete pod -l app.kubernetes.io/name=reclaimerr
```

Revert the commits and push; Flux will prune the Kustomization (`prune: true`) and VolSync will snapshot the PVC per the volsync component defaults.
