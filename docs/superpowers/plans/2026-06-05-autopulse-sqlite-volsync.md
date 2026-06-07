# Autopulse v2 Minimal Enable (SQLite + VolSync) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the dormant `autopulse` app online on image `v2.0.0` in a minimal configuration — pod Ready, web UI reachable, SQLite database persisted and backed up via VolSync, no triggers/targets yet.

**Architecture:** GitOps via Flux. Edit the autopulse manifests on branch `feat/autopulse-sqlite-volsync`, validate offline with `flate` (resolves Flux `postBuild` substitutions and renders HelmReleases without a cluster), create the one new secret in 1Password, enable the app in the namespace kustomization, then merge to `main` so Flux reconciles and deploys. Verify against the live cluster.

**Tech Stack:** Flux v2, Kustomize Components, bjw-s app-template HelmRelease, External Secrets Operator + 1Password Connect, VolSync (kopia→NFS, restic→R2) on Ceph RBD, `flate` for offline validation, `just` task runner under `mise`.

**Conventions for this plan:**
- Run all commands from the repo root: `~/GIT/LukeEvansTech/talos-cluster`.
- The repo uses `mise`; `just` recipes need the mise toolchain on PATH. If `just` is not found, prefix with `mise exec -- ` (e.g. `mise exec -- just kube flate-test`).
- `KUBECONFIG` is set by mise to the repo-root `kubeconfig`. For raw `kubectl`, export it first: `export KUBECONFIG="$PWD/kubeconfig"`.
- The branch `feat/autopulse-sqlite-volsync` already exists and holds the committed design doc. All work happens there.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `kubernetes/apps/media/autopulse/app/externalsecret.yaml` | Render `autopulse-secret` `config.yaml` (auth from 1Password, SQLite URL inline) | Modify — inline template, drop missing-ConfigMap indirection |
| `kubernetes/apps/media/autopulse/app/helmrelease.yaml` | App workload; mount writable PVC at `/app/data` | Modify — add `persistence.data` |
| `kubernetes/apps/media/autopulse/ks.yaml` | Flux Kustomization; add VolSync component + capacity | Modify — add component + `VOLSYNC_CAPACITY` |
| `kubernetes/apps/media/autopulse/app/resources/` | (empty, unreferenced) | Delete |
| `kubernetes/apps/media/kustomization.yaml` | Namespace app list | Modify — un-comment autopulse, drop stale TODO |
| 1Password item `autopulse` | Auth username/password | Create (manual, one-off) |

---

### Task 1: Inline the ExternalSecret config

**Files:**
- Modify: `kubernetes/apps/media/autopulse/app/externalsecret.yaml`

- [ ] **Step 1: Replace the file contents**

Replace the entire file with:

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: autopulse-secret
spec:
  refreshInterval: 5m
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: autopulse-secret
    template:
      engineVersion: v2
      data:
        config.yaml: |
          app:
            database_url: sqlite://data/autopulse.db
          auth:
            username: "{{ .username }}"
            password: "{{ .password }}"
  dataFrom:
    - extract:
        key: autopulse
```

Rationale: the previous `templateFrom.configMap: autopulse-config` referenced a ConfigMap that exists nowhere in git or the cluster (the only other user, `garage`, has the same gap). Inlining removes the broken indirection. `database_url` is non-secret so it lives in the template; only `username`/`password` come from the `autopulse` 1Password item.

- [ ] **Step 2: Verify YAML parses**

Run: `yq '.spec.target.template.data["config.yaml"]' kubernetes/apps/media/autopulse/app/externalsecret.yaml`
Expected: prints the multi-line config.yaml block (app/auth sections), no parse error.

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/autopulse/app/externalsecret.yaml
git commit -m "feat(autopulse): inline config.yaml in externalsecret (sqlite)"
```

---

### Task 2: Add the writable data volume to the HelmRelease

**Files:**
- Modify: `kubernetes/apps/media/autopulse/app/helmrelease.yaml`

- [ ] **Step 1: Add `persistence.data`**

In `spec.values.persistence`, add a `data` entry (alongside the existing `config`, `media`, `tmp`). Insert it as the first child of `persistence:` so the block reads:

```yaml
    persistence:
      data:
        existingClaim: "{{ .Release.Name }}"
        globalMounts:
          - path: /app/data
      config:
        type: secret
        name: autopulse-secret
        globalMounts:
          - path: /app/config.yaml
            subPath: config.yaml
            readOnly: true
      media:
        type: nfs
        server: nas01.${SECRET_INTERNAL_DOMAIN}
        path: /mnt/data/media
        globalMounts:
          - path: /mnt/data/media
            readOnly: true
      tmp:
        type: emptyDir
        globalMounts:
          - path: /tmp
```

Leave everything else unchanged — in particular keep `securityContext.readOnlyRootFilesystem: true` (the new PVC is writable, so SQLite at `/app/data/autopulse.db` works). The `existingClaim` name `{{ .Release.Name }}` resolves to `autopulse`, which is the PVC the VolSync component creates in Task 3.

- [ ] **Step 2: Verify the persistence block**

Run: `yq '.spec.values.persistence | keys' kubernetes/apps/media/autopulse/app/helmrelease.yaml`
Expected: `["data", "config", "media", "tmp"]`

Run: `yq '.spec.values.persistence.data.globalMounts[0].path' kubernetes/apps/media/autopulse/app/helmrelease.yaml`
Expected: `/app/data`

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/autopulse/app/helmrelease.yaml
git commit -m "feat(autopulse): mount volsync data pvc at /app/data"
```

---

### Task 3: Add the VolSync component to the Flux Kustomization

**Files:**
- Modify: `kubernetes/apps/media/autopulse/ks.yaml`

- [ ] **Step 1: Replace the file contents**

Replace the entire file with (adds the `volsync` component and the `VOLSYNC_CAPACITY` substitution; everything else is preserved):

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app autopulse
  namespace: &namespace media
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  components:
    - ../../../../components/gatus/guarded
    - ../../../../components/homepage
    - ../../../../components/volsync
  interval: 1h
  path: ./kubernetes/apps/media/autopulse/app
  postBuild:
    substitute:
      APP: *app
      VOLSYNC_CAPACITY: 1Gi
      HOMEPAGE_NAME: Autopulse
      HOMEPAGE_GROUP: Media
      HOMEPAGE_ICON: autopulse.svg
      HOMEPAGE_DESCRIPTION: Media automation
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

`VOLSYNC_CAPACITY: 1Gi` because the SQLite DB is tiny (the component default is 5Gi). All other VolSync knobs use component defaults: storageClass `ceph-block`, snapshotClass `csi-ceph-blockpool`, backups to NFS (kopia) + R2 (restic) from the shared `volsync-template`/`volsync-r2-template` 1Password items.

- [ ] **Step 2: Verify substitutions and components**

Run: `yq '.spec.components, .spec.postBuild.substitute' kubernetes/apps/media/autopulse/ks.yaml`
Expected: components list includes `../../../../components/volsync`; substitute map includes `APP: autopulse` and `VOLSYNC_CAPACITY: 1Gi`.

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/autopulse/ks.yaml
git commit -m "feat(autopulse): add volsync component (1Gi sqlite pvc)"
```

---

### Task 4: Remove the empty resources directory

**Files:**
- Delete: `kubernetes/apps/media/autopulse/app/resources/`

- [ ] **Step 1: Confirm it is empty, then remove**

```bash
ls -A kubernetes/apps/media/autopulse/app/resources/   # expect: no output (empty)
rmdir kubernetes/apps/media/autopulse/app/resources/
```

(Empty dirs are not tracked by git, so there is nothing to commit. If `ls` shows files, stop — the dir is not empty and the assumption is wrong; investigate before deleting.)

---

### Task 5: Create the autopulse 1Password item

**Files:** none (external system). Must exist before the ExternalSecret reconciles, or it stays `SecretSyncedError` (non-fatal; ESO retries every 5m).

- [ ] **Step 1: Identify the vault the Connect server reads**

In a 1Password-authenticated shell (the operator's `!` shell after `eval $(op signin)`), find the vault that already holds cluster secrets:

```bash
op item get volsync-template --format json | yq '.vault.name'
```
Expected: the vault name (e.g. `Kubernetes`). Use that as `<VAULT>` below.

- [ ] **Step 2: Create the item with a generated password**

```bash
op item create \
  --category login \
  --vault '<VAULT>' \
  --title autopulse \
  username=admin \
  --generate-password='letters,digits,symbols,32'
```
Expected: item `autopulse` created with fields `username=admin` and a generated `password`.

- [ ] **Step 3: Verify the fields ESO will extract**

```bash
op item get autopulse --format json | yq '.fields[] | select(.label == "username" or .label == "password") | .label'
```
Expected: prints `username` and `password`. (These map to the `{{ .username }}` / `{{ .password }}` template vars in Task 1.)

---

### Task 6: Enable autopulse in the namespace kustomization

**Files:**
- Modify: `kubernetes/apps/media/kustomization.yaml`

- [ ] **Step 1: Un-comment the app and drop the stale TODO**

Replace these two lines:

```yaml
  # TODO: Update externalsecret with autopulse config before enabling
  # - ./autopulse/ks.yaml
```

with:

```yaml
  - ./autopulse/ks.yaml
```

(Keep alphabetical/list ordering as-is — it currently sits between `audiobookshelf` and `bazarr`, which is correct.)

- [ ] **Step 2: Verify the entry is active**

Run: `yq '.resources[] | select(. == "./autopulse/ks.yaml")' kubernetes/apps/media/kustomization.yaml`
Expected: `./autopulse/ks.yaml`

Run: `grep -n "autopulse config before enabling" kubernetes/apps/media/kustomization.yaml`
Expected: no output (the TODO is gone).

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/media/kustomization.yaml
git commit -m "feat(autopulse): enable in media kustomization"
```

---

### Task 7: Validate offline with flate

**Files:** none (validation only).

- [ ] **Step 1: Render the autopulse Kustomization (postBuild resolved)**

Run: `just kube flate-build-ks media autopulse`
(If `just` is not found: `mise exec -- just kube flate-build-ks media autopulse`.)
Expected: clean YAML output with no `${...}` left unresolved; includes the `autopulse` PVC (storage `1Gi`), `ReplicationSource` `autopulse-nfs` and `autopulse-r2`, `ReplicationDestination`, the `ExternalSecret`, and the HelmRelease. Exit code 0.

- [ ] **Step 2: Render the HelmRelease**

Run: `just kube flate-build-hr media autopulse`
Expected: a Deployment with the `app` container image `ghcr.io/dan-online/autopulse:v2.0.0...`, a volume/volumeMount for `data` at `/app/data` backed by claim `autopulse`, and `readOnlyRootFilesystem: true`. Exit code 0.

- [ ] **Step 3: Run the full offline test suite**

Run: `just kube flate-test`
Expected: all manifests pass (flux-local equivalent), exit code 0. Investigate any autopulse-related failure before proceeding.

---

### Task 8: Push, open PR, pass lint, merge

**Files:** none (delivery). Flux reconciles `main`, so the change deploys on merge.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/autopulse-sqlite-volsync
```

- [ ] **Step 2: Open a PR**

```bash
gh pr create --title "feat(autopulse): enable v2 minimal on sqlite + volsync" \
  --body "Brings the dormant autopulse app online on v2.0.0 in a minimal config (no triggers/targets). SQLite on a 1Gi VolSync-backed PVC mounted at /app/data; inlines config.yaml in the ExternalSecret (drops the missing autopulse-config ConfigMap indirection); enables it in the media kustomization. Design: docs/superpowers/specs/2026-06-05-autopulse-sqlite-volsync-design.md"
```
Expected: PR URL printed.

- [ ] **Step 3: Wait for the Lint check, then merge**

Run: `gh pr checks --watch`
Expected: the `Lint` (super-linter) check passes. Then:

```bash
gh pr merge --squash --delete-branch
```
Expected: PR merged to `main`, remote branch deleted. (Per house convention `--auto` is disabled; use `--admin` only if branch protection blocks and the Lint check already passed.)

- [ ] **Step 4: Sync local main**

```bash
git checkout main && git pull --ff-only
```

---

### Task 9: Reconcile and verify on the cluster

**Files:** none (verification). All commands need `export KUBECONFIG="$PWD/kubeconfig"` first.

- [ ] **Step 1: Force a fast reconcile (optional — Flux polls within ~1h)**

```bash
export KUBECONFIG="$PWD/kubeconfig"
flux reconcile source git flux-system
flux reconcile kustomization media --with-source
```
Expected: reconciliation completes without error.

- [ ] **Step 2: Verify the ExternalSecret synced**

Run: `kubectl -n media get externalsecret autopulse-secret`
Expected: `STATUS=SecretSynced`, `READY=True`. (If `SecretSyncedError`, the 1Password item/vault from Task 5 is wrong — fix and `just kube sync-es`.)

- [ ] **Step 3: Verify the PVC bound and VolSync objects exist**

```bash
kubectl -n media get pvc autopulse
kubectl -n media get replicationsource
```
Expected: PVC `autopulse` `STATUS=Bound` (1Gi, `ceph-block`); ReplicationSources `autopulse-nfs` and `autopulse-r2` present.

- [ ] **Step 4: Verify the pod is Ready and the DB was created**

```bash
kubectl -n media get pods -l app.kubernetes.io/name=autopulse
kubectl -n media logs deploy/autopulse | tail -40
```
Expected: pod `READY 1/1`, `STATUS=Running`; logs show the server starting, SQLite DB created at `/app/data/autopulse.db`, and no auth/config-load errors.

  - Fallback if logs show a config-load error about missing `triggers`/`targets`: add `triggers: {}` and `targets: {}` under the inline config in `externalsecret.yaml` (Task 1), commit, push to a fix branch, merge, and re-reconcile.

- [ ] **Step 5: Verify the web UI route answers**

```bash
kubectl -n media exec deploy/autopulse -- wget -qO- http://localhost:2875/health || true
```
Expected: a healthy response from the public `/health` endpoint (v2 keeps it unauthenticated). Then confirm `https://autopulse.<internal-domain>` loads the v2 web UI in a browser.

- [ ] **Step 6: Confirm Flux Kustomization is Ready**

Run: `flux -n media get kustomization autopulse`
Expected: `READY=True`, applied revision matches `main`.

---

## Self-Review

**Spec coverage:**
- SQLite database_url → Task 1. ✓
- Writable `/app/data` + `readOnlyRootFilesystem` kept → Task 2. ✓
- VolSync PVC + backups, 1Gi → Task 3. ✓
- Inline config (drop missing ConfigMap) → Task 1. ✓
- Auth secret in 1Password (generate + `op item create`) → Task 5. ✓
- Enable in namespace kustomization, remove TODO → Task 6. ✓
- Delete empty `resources/` → Task 4. ✓
- Verification (ES synced, PVC bound, pod Ready, DB created, UI reachable, VolSync sources) → Task 9. ✓
- Risk: empty triggers/targets → mitigation in Task 9 Step 4 fallback. ✓
- Risk: first-deploy restore finds no snapshot → standard bootstrap, PVC provisions empty (no action). ✓
- Risk: wrong vault → Task 9 Step 2 note. ✓

**Placeholder scan:** `<VAULT>` in Task 5 is an intentional, explicitly-resolved value (Step 1 derives it). No TBD/TODO-to-fill remain.

**Type consistency:** PVC/claim name `autopulse` (from `APP`) == HelmRelease `existingClaim: {{ .Release.Name }}` == ReplicationSource `sourcePVC`. Secret name `autopulse-secret` consistent across ExternalSecret target and HelmRelease config mount. Template vars `username`/`password` match the 1Password field labels created in Task 5. ✓
