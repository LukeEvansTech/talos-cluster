# Agents Config Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port joryirving/home-ops `.agents/` (sorting instructions + add-app skill) into this repo, adapted to its single-cluster / per-app-`ocirepository` conventions, with `.claude/` symlinks and an `AGENTS.md → CLAUDE.md` symlink.

**Architecture:** Canonical content under `.agents/`; Claude Code discovers the skill via a relative dir symlink `.claude/skills/add-app -> ../../.agents/skills/add-app`; sorting instructions are reachable via a one-line `CLAUDE.md` pointer; `AGENTS.md` is a root symlink to `CLAUDE.md` for non-Claude tools.

**Tech Stack:** Markdown, git symlinks (mode 120000), lefthook/super-linter, flate (for the *scaffolded apps*, not for these docs).

**Spec:** `docs/superpowers/specs/2026-06-15-agents-config-port-design.md`

**Verification note:** These deliverables are markdown + symlinks — there is no unit-test suite. "Verify" steps mean: file reads back correctly, symlinks resolve, `git ls-files -s` shows mode `120000`, and markdown lints clean.

---

### Task 1: Sorting instructions

**Files:**
- Create: `.agents/instructions/sorting.instructions.md`

Adapted from joryirving's file. Two substantive changes vs upstream: (a) app-template detection rule rewritten for this repo's per-app `OCIRepository`; (b) HelmRelease `spec` ordering puts `interval` **before** `chartRef` (this repo's actual convention — verified across HRs like `error-pages`, `mosquitto`, `karakeep`).

- [ ] **Step 1: Create the file**

Write `.agents/instructions/sorting.instructions.md` with exactly:

````markdown
# Sorting instructions for all yaml files

Whenever asked to sort these files, follow these instructions:

- **Default rule**: All fields and properties should be sorted alphabetically at every level of the YAML structure, regardless of how deeply nested they are, unless a specific override rule is provided below or in other applicable instructions files.
- All yaml files should start with `---` at the top of the document.
- All documents should have a `YAML` LSP schema associated with them, if possible, to enable validation and auto-completion features in editors that support it. This is especially important for Kubernetes-related files, which should use the appropriate Kubernetes schema based on their `apiVersion` and `kind` fields. It should be below the `---` at the top of the document.

## Override rules for Kubernetes related file types

- Whenever they are present on the same level of a YAML structure, these fields should be sorted as follows:
  - `apiVersion`
  - `kind`
  - `metadata`
  - `spec`

- The items within the `metadata` section should be sorted as follows:
  - `name`
  - `namespace`
  - `annotations`
  - `labels`

## HelmRelease rules for app-template

This section gives instructions specifically for HelmReleases based on the bjw-s `app-template` chart.

In this repository, app-template apps do **not** share a single chart. Each app has its own
`app/ocirepository.yaml` whose `url` ends in `bjw-s-labs/helm/app-template`, and the HelmRelease
references it via `spec.chartRef.kind: OCIRepository` with `spec.chartRef.name: <app>` (the app's own
name). Identify an app-template HelmRelease by that per-app `OCIRepository` source — **not** by a
shared `chartRef.name: app-template`.

### Sorting rules

Whenever asked to sort these files, follow these instructions:

- Whenever there is an `enabled` field, it should be the first field within its section, unless a more specific rule below dictates otherwise.

- The items within the `spec` section should be sorted as follows (this repo orders `interval` before `chartRef`):
  - `interval`
  - `chartRef`
  - `dependsOn` (if present)
  - `install` (if present)
  - `upgrade` (if present)
  - `values`

- Items within the `spec.values` section should be sorted as follows:
  - `defaultPodOptions` (if present)
  - All sibling keys at the `spec.values` level should be sorted alphabetically (e.g., `controllers`, `persistence`, `route`, `service`)

Note: Sibling keys within `persistence.*`, `service.*`, `route.*`, `configMaps.*`, etc. are NOT required to be sorted - only the keys within each individual item. For example, if `persistence` has `config`, `data`, and `tmpfs` as children, they can be in any order. Only the keys within `persistence.config`, `persistence.data`, etc. should be sorted.

**Important:** The sorting rules apply to the HelmRelease structure itself. Do NOT sort arbitrary YAML content embedded within string fields (e.g., `configMap.data.*` values containing YAML configurations).

### General pattern for section keys

Unless a more specific rule applies, keys within any section should be ordered as:

- `annotations` (if present)
- `labels` (if present)
- All other keys should be sorted alphabetically

### Detailed sorting rules for nested sections

- Items within the `spec.values.controllers.*` sections should be sorted as follows:
  - `type` (if present, always first)
  - `annotations` (if present)
  - `labels` (if present)
  - Controller-specific fields such as `cronjob` or `statefulset` (if present)
  - `pod`
  - Any other fields should be sorted alphabetically, except the following fields which should come last (and in this order):
  - `initContainers` (if present)
  - `containers` (if present)

- Items within `spec.values.controllers.*.containers.*` sections should be sorted as follows:
  - `image`
  - Any other fields should be added next in alphabetical order.

- Items within `spec.values.controllers.*.containers.resources` and `spec.values.controllers.*.initContainers.resources` sections should be sorted as follows:
  - `requests`
  - `limits`

- Items within `spec.values.service.*` sections should be sorted as follows:
  - `type` (if present)
  - `annotations` (if present)
  - `labels` (if present)
  - Any other fields should be added next in alphabetical order.

- Items within `persistence.*` sections should be sorted as follows:
  - `type` (if present)
  - `annotations` (if present)
  - `labels` (if present)
  - Any other fields should be sorted alphabetically, except the following fields which should come last (and in this order):
  - `globalMounts` (if present)
  - `advancedMounts` (if present)

### Quick reference

**Before sorting, verify the chart is app-template based:**

1. Check the app's `app/ocirepository.yaml` `url` ends in `bjw-s-labs/helm/app-template` (and the HelmRelease `spec.chartRef` points at that `OCIRepository`).
2. If not app-template, do not apply these sorting rules.

**Decision tree for sorting HelmRelease fields:**

```
At spec level?
  → interval, chartRef, dependsOn, install, upgrade, values

At spec.values level?
  → defaultPodOptions first (if present), then alphabetical

Within controllers.*.containers.* or .initContainers.*?
  → image first, then alphabetical

Within persistence.*, service.*, etc. siblings?
  → No: Do not sort siblings (e.g., persistence.config vs persistence.data order doesn't matter)
  → Yes: Sort keys within each item (type → annotations → labels → alphabetical)
```
````

- [ ] **Step 2: Verify it reads back and lints**

Run: `head -5 .agents/instructions/sorting.instructions.md`
Expected: starts with `# Sorting instructions for all yaml files`.

- [ ] **Step 3: Commit**

```bash
git add .agents/instructions/sorting.instructions.md
git commit -m "feat(agents): add adapted yaml sorting instructions"
```

---

### Task 2: add-app skill

**Files:**
- Create: `.agents/skills/add-app/SKILL.md`

- [ ] **Step 1: Create the skill file**

Write `.agents/skills/add-app/SKILL.md` with exactly:

````markdown
---
name: add-app
description: Scaffold a new bjw-s app-template application for the talos-cluster repository
---

# Add New Application

Scaffold a new application for this repository's **single-cluster** Flux layout.

## Repository-specific assumptions

- App manifests live in `kubernetes/apps/<namespace>/<app>/`: a Flux entrypoint `ks.yaml` plus the
  rendered resources under `app/`.
- Each app-template app has its **own** `app/ocirepository.yaml` pointing at
  `oci://ghcr.io/bjw-s-labs/helm/app-template`; the HelmRelease references it via
  `spec.chartRef.kind: OCIRepository`, `name: <app>` (the app's own name). There is **no** shared
  `app-template` OCIRepository — do not create one, and do not use `chartRef.name: app-template`.
- The app is registered (alphabetically) in `kubernetes/apps/<namespace>/kustomization.yaml`.
- Secrets use `external-secrets` with the `onepassword-connect` `ClusterSecretStore` (reads the
  `Talos` 1Password vault). ES-using apps `dependsOn` `onepassword-connect`.
- Ingress is Envoy Gateway via the app-template `route:` values (preferred) on the
  `envoy-internal` / `envoy-external` listeners in the `network` namespace.
- This repo is **PUBLIC** — never write LAN IPs, `.lan`/`.internal` hostnames, or device names.
  Hosts use `${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}` (Flux substitutes them at apply time).
  Any literal `${VAR}` that must survive Flux substitution has to be escaped as `$${VAR}`.

## Workflow

### Step 1: Collect application details

Use the **AskUserQuestion** tool to gather:

1. App name
2. Namespace (existing dir under `kubernetes/apps/`, e.g. `default`, `media`, `downloads`, `observability`)
3. Image repository
4. Image tag
5. app-template chart version for `ocirepository.yaml` `ref.tag`
6. Primary service port
7. Whether the app needs an `ExternalSecret`
8. Whether the app needs persistence (PVC `existingClaim`, `emptyDir`, NFS, …)
9. Whether the app needs a route, and if so internal-only (`envoy-internal`) or external (`envoy-external`)
10. Any Flux `dependsOn` entries, and whether it uses components (`gatus/guarded`, `volsync`, `alerts`, `homepage`)

Always confirm before writing files.

### Step 2: Inspect neighbouring apps

Before generating files:

1. Read 1–2 existing apps in the **same namespace** (`kubernetes/apps/<namespace>/*/`).
2. Match local patterns for probes, persistence, routes, resources, and secret templates.
3. Note the namespace's `kustomization.yaml` components (typically `global-vars` + `alerts`).

### Step 3: Create the app directory

Create `kubernetes/apps/<namespace>/<app>/` with `ks.yaml`, `app/kustomization.yaml`,
`app/ocirepository.yaml`, `app/helmrelease.yaml`. Add `app/externalsecret.yaml` only if secrets are
needed. Prefer the inline `route:` value over a standalone `app/httproute.yaml` (the latter is the
rarer case, e.g. catch-all routes).

### Step 4: Generate the Flux Kustomization (`ks.yaml`)

`kubernetes/apps/<namespace>/<app>/ks.yaml`:

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app <app>
  namespace: &namespace <namespace>
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  interval: 1h
  path: ./kubernetes/apps/<namespace>/<app>/app
  postBuild: {}
  prune: true
  retryInterval: 2m
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system
  targetNamespace: *namespace
  timeout: 5m
  wait: true
```

If the app uses an `ExternalSecret`, add a `dependsOn` (alphabetically, after `commonMetadata`):

```yaml
  dependsOn:
    - name: onepassword-connect
      namespace: external-secrets
```

If the app uses components (gatus/volsync/etc.), add `spec.components` and replace `postBuild: {}`
with the matching substitutes. Component paths are `../../../../components/...` relative to the app:

```yaml
  components:
    - ../../../../components/gatus/guarded
    - ../../../../components/volsync
  postBuild:
    substitute:
      APP: *app
      VOLSYNC_CAPACITY: 5Gi
```

The `volsync` component goes in `ks.yaml` `spec.components` **only** — never also in
`app/kustomization.yaml` (Flux applies ks.yaml components on top of the path build; listing in both
double-applies). Keep `spec` keys alphabetical (this repo's ks.yaml convention).

### Step 5: Generate `app/kustomization.yaml`

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./ocirepository.yaml
  - ./helmrelease.yaml
```

Add `./externalsecret.yaml` and/or `./httproute.yaml` lines only when those files exist.

### Step 6: Generate `app/ocirepository.yaml`

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: <app>
spec:
  interval: 1h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: <chart-version>
  url: oci://ghcr.io/bjw-s-labs/helm/app-template
```

### Step 7: Generate `app/helmrelease.yaml`

`spec` order is `interval` → `chartRef` → (`dependsOn`) → `values` (this repo omits `install`/`upgrade`
on most HRs — the root kustomization injects those defaults):

```yaml
---
# yaml-language-server: $schema=https://raw.githubusercontent.com/bjw-s-labs/helm-charts/main/charts/other/app-template/schemas/helmrelease-helm-v2.schema.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: <app>
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: <app>
  values:
    controllers:
      <app>:
        containers:
          app:
            image:
              repository: <image-repository>
              tag: <image-tag>
            probes:
              liveness:
                enabled: true
              readiness:
                enabled: true
            resources:
              requests:
                cpu: 10m
              limits:
                memory: 512Mi
            securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: true
              capabilities:
                drop:
                  - ALL
    defaultPodOptions:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
    service:
      app:
        ports:
          http:
            port: <port>
```

If the app needs a route, add (internal-only default — both hostnames resolve via `envoy-internal`):

```yaml
    route:
      app:
        hostnames:
          - "{{ .Release.Name }}.${SECRET_DOMAIN}"
        parentRefs:
          - name: envoy-internal
            namespace: network
```

For **external** exposure, use `name: envoy-external` instead (and confirm the intent — this repo is
public). For GPU workloads, set `runtimeClassName: nvidia` under the controller's `pod`.

### Step 8: Generate `app/externalsecret.yaml` (only if needed)

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/external-secrets.io/externalsecret_v1.json
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: <app>
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: onepassword-connect
  target:
    name: <app>-secret
    template:
      data: {}
  dataFrom:
    - extract:
        key: <app>
```

Mirror the templated `target.template.data` keys from a similar existing app rather than forcing a
generic template. The 1Password item must live in the `Talos` vault.

### Step 9: Register the app in the namespace kustomization

Add the app's `ks.yaml` to `kubernetes/apps/<namespace>/kustomization.yaml` `resources`, keeping the
list alphabetical. Match the exact reference style used by neighbours (e.g. `./<app>/ks.yaml`).

### Step 10: Verify

1. `kubernetes/apps/<namespace>/<app>/` contains `ks.yaml` and `app/` with the expected files.
2. The namespace `kustomization.yaml` references the new app.
3. The HelmRelease `chartRef` points at the per-app `OCIRepository`, not a shared `app-template`.
4. No plain-text secrets, LAN IPs, or internal hostnames were introduced.
5. Render and validate with flate:

   ```bash
   flate build hr <app> -n <namespace> --path kubernetes/flux/cluster
   flate test all --path kubernetes/flux/cluster --allow-missing-secrets
   ```

## Notes

- Do **not** create a shared `app-template` OCIRepository; each app gets its own `app/ocirepository.yaml`.
- Prefer minimal scaffolding that matches existing apps in the same namespace.
- If the workload is not a good fit for `app-template`, stop and ask the user before continuing.
````

- [ ] **Step 2: Verify frontmatter + headings**

Run: `head -6 .agents/skills/add-app/SKILL.md`
Expected: YAML frontmatter with `name: add-app` and `description:`.

- [ ] **Step 3: Commit**

```bash
git add .agents/skills/add-app/SKILL.md
git commit -m "feat(agents): add adapted add-app scaffolding skill"
```

---

### Task 3: Symlinks (.claude discovery + AGENTS.md)

**Files:**
- Create symlink: `.claude/skills/add-app -> ../../.agents/skills/add-app`
- Create symlink: `AGENTS.md -> CLAUDE.md`

- [ ] **Step 1: Create the symlinks**

```bash
mkdir -p .claude/skills
ln -s ../../.agents/skills/add-app .claude/skills/add-app
ln -s CLAUDE.md AGENTS.md
```

- [ ] **Step 2: Verify they resolve and git records them as symlinks**

```bash
head -3 .claude/skills/add-app/SKILL.md   # resolves through the link
head -3 AGENTS.md                          # shows CLAUDE.md content
```
Expected: skill frontmatter, and CLAUDE.md's first lines, respectively.

```bash
git add .claude/skills/add-app AGENTS.md
git ls-files -s .claude/skills/add-app AGENTS.md
```
Expected: both lines start with mode `120000` (git symlink), not `100644`.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(agents): symlink add-app into .claude and AGENTS.md to CLAUDE.md"
```

---

### Task 4: CLAUDE.md pointer to sorting instructions

**Files:**
- Modify: `CLAUDE.md` (add one pointer line referencing the sorting instructions)

- [ ] **Step 1: Add the pointer**

In `CLAUDE.md`, under the existing "Key Conventions" section (which already discusses YAML anchors
and schema comments), add a bullet:

```markdown
- YAML sorting conventions (alphabetical defaults + app-template ordering) are documented in `.agents/instructions/sorting.instructions.md`; apply them when asked to sort YAML
```

Place it adjacent to the existing YAML-anchor / schema-comment bullets so it reads naturally.

- [ ] **Step 2: Verify**

Run: `grep -n "sorting.instructions.md" CLAUDE.md`
Expected: one match showing the new bullet.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: point CLAUDE.md at .agents sorting instructions"
```

---

### Task 5: Final verification

- [ ] **Step 1: Tree + symlink sanity**

```bash
find .agents -type f
ls -l AGENTS.md .claude/skills/add-app
git ls-files -s | grep '^120000'
```
Expected: the two markdown files listed; both symlinks shown; two `120000` entries.

- [ ] **Step 2: Lint the new markdown (mirror CI if convenient)**

The repo's lint is super-linter via `just lint` (amd64 image, slow on Apple Silicon). Optional for a
docs-only change; if run, expect markdownlint/prettier to pass for the new files. At minimum confirm
no tabs / trailing-whitespace issues introduced.

- [ ] **Step 3: Confirm clean status**

```bash
git status --short      # expect empty (all committed)
git log --oneline -6
```

---

## Self-review notes

- **Spec coverage:** sorting (Task 1), add-app (Task 2), `.claude` + `AGENTS.md` symlinks (Task 3),
  CLAUDE.md pointer (Task 4), verification (Task 5) — every spec section maps to a task. `pr-review`
  is intentionally absent (skipped). The "worth flagging later" items are intentionally not built.
- **No placeholders:** `<app>` / `<namespace>` / `<image-…>` / `<port>` / `<chart-version>` are the
  skill's own template tokens (the skill fills them at scaffold time), not plan gaps.
- **Consistency:** HR `spec` ordering (`interval` → `chartRef` → … → `values`), `onepassword-connect`
  store name, `target.name: <app>-secret`, and component paths (`../../../../components/...`) are used
  identically in the sorting file, the skill, and the spec.
