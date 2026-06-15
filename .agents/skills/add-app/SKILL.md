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
