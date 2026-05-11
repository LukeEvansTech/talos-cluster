# iSponsorBlockTV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy iSponsorBlockTV as a headless background daemon in the media namespace with VolSync-backed persistent config.

**Architecture:** Standard bjw-s/app-template deployment following existing media namespace patterns. No service/route (headless daemon). VolSync for config persistence across cluster rebuilds.

**Tech Stack:** Flux CD, bjw-s/app-template 4.6.2, VolSync, Kustomize

**Spec:** `docs/superpowers/specs/2026-03-24-isponsorblocktv-design.md`

---

## File Structure

| Action | File                                                           | Purpose                                   |
| ------ | -------------------------------------------------------------- | ----------------------------------------- |
| Create | `kubernetes/apps/media/isponsorblocktv/ks.yaml`                | Flux Kustomization with VolSync component |
| Create | `kubernetes/apps/media/isponsorblocktv/app/kustomization.yaml` | Resource list for app directory           |
| Create | `kubernetes/apps/media/isponsorblocktv/app/helmrelease.yaml`   | HelmRelease with bjw-s/app-template       |
| Create | `kubernetes/apps/media/isponsorblocktv/app/ocirepository.yaml` | OCI chart source                          |
| Modify | `kubernetes/apps/media/kustomization.yaml`                     | Add isponsorblocktv ks.yaml to resources  |

---

### Task 1: Create OCI Repository

**Files:**

- Create: `kubernetes/apps/media/isponsorblocktv/app/ocirepository.yaml`

- [ ] **Step 1: Create the OCIRepository manifest**

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
    name: isponsorblocktv
spec:
    interval: 1h
    layerSelector:
        mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
        operation: copy
    ref:
        tag: 4.6.2
    url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

Reference: `kubernetes/apps/media/plex-auto-languages/app/ocirepository.yaml`

- [ ] **Step 2: Validate YAML formatting**

Run: `yamlfmt kubernetes/apps/media/isponsorblocktv/app/ocirepository.yaml`

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/isponsorblocktv/app/ocirepository.yaml
git commit -m "feat(isponsorblocktv): add OCI repository for app-template chart"
```

---

### Task 2: Create HelmRelease

**Files:**

- Create: `kubernetes/apps/media/isponsorblocktv/app/helmrelease.yaml`

- [ ] **Step 1: Create the HelmRelease manifest**

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
    name: isponsorblocktv
spec:
    interval: 1h
    chartRef:
        kind: OCIRepository
        name: isponsorblocktv
    values:
        controllers:
            isponsorblocktv:
                annotations:
                    reloader.stakater.com/auto: "true"
                containers:
                    app:
                        image:
                            repository: ghcr.io/dmunozv04/isponsorblocktv
                            tag: v2.6.1@sha256:545856523283753ebcf4b400a46895b9906844be5265a0f4cab98a6b0bdf84be
                        env:
                            TZ: "${TIMEZONE}"
                        probes:
                            liveness:
                                enabled: false
                            readiness:
                                enabled: false
                            startup:
                                enabled: false
                        securityContext:
                            allowPrivilegeEscalation: false
                            readOnlyRootFilesystem: true
                            capabilities: { drop: ["ALL"] }
                        resources:
                            requests:
                                cpu: 10m
                                memory: 128Mi
                            limits:
                                memory: 256Mi
        defaultPodOptions:
            securityContext:
                runAsNonRoot: true
                runAsUser: 1000
                runAsGroup: 1000
                fsGroup: 1000
                fsGroupChangePolicy: OnRootMismatch
                seccompProfile: { type: RuntimeDefault }
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

Reference: `kubernetes/apps/media/plex-auto-languages/app/helmrelease.yaml` (headless daemon pattern, probes disabled)

Key differences from reference:

- No service/route (headless daemon with no ports)
- No envFrom/secretRef (no external secrets needed)
- PVC via existingClaim for VolSync (like metube pattern)
- Mount at `/app/data` (iSponsorBlockTV's data directory)

- [ ] **Step 2: Validate YAML formatting**

Run: `yamlfmt kubernetes/apps/media/isponsorblocktv/app/helmrelease.yaml`

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/isponsorblocktv/app/helmrelease.yaml
git commit -m "feat(isponsorblocktv): add HelmRelease for headless daemon deployment"
```

---

### Task 3: Create App Kustomization

**Files:**

- Create: `kubernetes/apps/media/isponsorblocktv/app/kustomization.yaml`

- [ ] **Step 1: Create the kustomization manifest**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
    - ./ocirepository.yaml
    - ./helmrelease.yaml
```

Note: No externalsecret.yaml listed (not needed for this app).

- [ ] **Step 2: Validate YAML formatting**

Run: `yamlfmt kubernetes/apps/media/isponsorblocktv/app/kustomization.yaml`

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/isponsorblocktv/app/kustomization.yaml
git commit -m "feat(isponsorblocktv): add app kustomization"
```

---

### Task 4: Create Flux Kustomization (ks.yaml)

**Files:**

- Create: `kubernetes/apps/media/isponsorblocktv/ks.yaml`

- [ ] **Step 1: Create the Flux Kustomization manifest**

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
    name: &app isponsorblocktv
    namespace: &namespace media
spec:
    commonMetadata:
        labels:
            app.kubernetes.io/name: *app
    components:
        - ../../../../components/volsync
    dependsOn:
        - name: rook-ceph-cluster
          namespace: rook-ceph
    interval: 1h
    path: ./kubernetes/apps/media/isponsorblocktv/app
    postBuild:
        substitute:
            APP: *app
            VOLSYNC_CAPACITY: 1Gi
            VOLSYNC_CACHE_CAPACITY: 1Gi
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

Reference: `kubernetes/apps/media/metube/ks.yaml` (VolSync pattern with rook-ceph dependency)

- [ ] **Step 2: Validate YAML formatting**

Run: `yamlfmt kubernetes/apps/media/isponsorblocktv/ks.yaml`

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/isponsorblocktv/ks.yaml
git commit -m "feat(isponsorblocktv): add Flux Kustomization with VolSync"
```

---

### Task 5: Register App in Media Namespace

**Files:**

- Modify: `kubernetes/apps/media/kustomization.yaml`

- [ ] **Step 1: Add isponsorblocktv to the resources list**

Add `- ./isponsorblocktv/ks.yaml` in alphabetical order (after `./imagemaid/ks.yaml`, before `./jellyfin/ks.yaml`).

- [ ] **Step 2: Validate YAML formatting**

Run: `yamlfmt kubernetes/apps/media/kustomization.yaml`

- [ ] **Step 3: Validate the Flux kustomization builds correctly**

Run: `flux-local build ks --path kubernetes/flux/cluster isponsorblocktv`

Expected: Successful build output showing the rendered manifests.

- [ ] **Step 4: Commit**

```bash
git add kubernetes/apps/media/kustomization.yaml
git commit -m "feat(isponsorblocktv): register app in media namespace"
```

---

### Task 6: Post-Deployment Device Pairing

This task is manual and happens after the manifests are pushed and reconciled by Flux.

- [ ] **Step 1: Verify the pod is running**

Run: `kubectl get pods -n media -l app.kubernetes.io/name=isponsorblocktv`

Expected: 1 pod in Running state (may be CrashLoopBackOff until config exists — this is expected).

- [ ] **Step 2: Run interactive setup to pair devices**

Run: `kubectl exec -it -n media deploy/isponsorblocktv -- iSPBTV setup`

Follow the TUI prompts to pair 2 Apple TVs and 3 LG Smart TVs using YouTube TV link codes.

- [ ] **Step 3: Verify the app is running with paired devices**

Run: `kubectl logs -n media deploy/isponsorblocktv --tail=20`

Expected: Log output showing connected devices and sponsor segment skipping activity.

- [ ] **Step 4: Trigger a VolSync snapshot to protect the config**

Run: `just kube snapshot`

This ensures the newly created config.json is backed up immediately.
