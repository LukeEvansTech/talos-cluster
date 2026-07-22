# Architecture overview

The cluster is a GitOps monorepo: everything that runs is declared in Git, and Flux reconciles it
onto Talos Linux nodes. There is no manual `kubectl apply` in normal operation.

## GitOps flow

```text
Git push → Flux detects change → reconciles Kustomizations → deploys HelmReleases → workloads run
```

The top-level Kustomization (`kubernetes/flux/cluster/ks.yaml`) recursively discovers every app under
`kubernetes/apps/` and applies default patches to each child, notably `postBuild.substituteFrom`
injecting both the `cluster-secrets` Secret and the `cluster-settings` ConfigMap
(`kubernetes/components/global-vars/cluster-settings.yaml`), and the HelmRelease
install/upgrade/rollback defaults.

## Repository layout

```text
kubernetes/
  apps/<namespace>/<app>/      # ks.yaml (Flux entry point) + app/ (HelmRelease, sources, routes)
  components/                  # reusable Kustomize components (global-vars, alerts, volsync, homepage, …)
  flux/cluster/                # core Flux bootstrap (root Kustomization with global patches)
bootstrap/                     # cluster bootstrap (just tasks + helmfile)
talos/                         # Talos machine config (talconfig.yaml + patches)
```

## App anatomy

Every app follows the same shape:

- `ks.yaml` is the Flux entry point. It uses YAML anchors (`&app`, `&namespace`, `*app`), sets
  `targetNamespace`, and lists any `components` (`volsync`, `alerts`, `homepage`) plus their
  `postBuild.substitute` values. (Gatus monitoring is automatic: the gatus-sidecar chart
  auto-discovers HTTPRoutes, so there is no per-app `gatus/guarded` component anymore.)
- Inside `app/`: a per-app chart source (`ocirepository.yaml` pointing at the bjw-s `app-template`
  for most apps), a `helmrelease.yaml`, an optional `externalsecret.yaml`, and usually an inline
  `route:` in the HelmRelease values rather than a standalone `httproute.yaml`.

## Key conventions

- Namespace `kustomization.yaml` files list apps (generally in alphabetical order) and reference
  the namespace's components.
- ConfigMaps set `metadata.namespace` explicitly (Checkov CKV_K8S_21 scans raw YAML before Flux
  applies `targetNamespace`).
- Flux `postBuild` replaces `${VAR}` against `cluster-secrets`/`cluster-settings`; undefined vars
  become empty strings, so any literal `${VAR}` you want preserved must be escaped as `$${VAR}`.
- GPU workloads set `runtimeClassName: nvidia`.
- This repository is public: internal addresses, node and device hostnames, and MACs are kept out of
  Git (a CI guard enforces this; see [Secrets](secrets.md)).
