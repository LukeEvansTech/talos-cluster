# talos-cluster — AI Assistant Guide

This is a **home Kubernetes cluster monorepo** managed with GitOps (Talos Linux, Flux, Renovate,
GitHub Actions). This file is the tool-agnostic conventions guide (read by Codex, Copilot, Cursor,
etc.). Claude Code loads it via an `@AGENTS.md` import in the repository's local `CLAUDE.md`.

> ⚠️ **This repository is PUBLIC.** Anything committed is world-visible. **Never commit** LAN IPs,
> `.lan` / `.internal` hostnames, device names, deployment topology, MACs, disk serials, or
> vendor-specific identifiers that map the home network. Use the `${SECRET_DOMAIN}` /
> `${SECRET_INTERNAL_DOMAIN}` placeholders (Flux substitutes them from `cluster-secrets` at
> apply-time). Address/lookup tables that need real device addresses must be templated inside an
> `ExternalSecret`'s `target.template.data` block and mounted from the rendered Secret — never
> rendered into a ConfigMap in git.

## Repository structure

```text
kubernetes/
├── apps/                  # App manifests by namespace
│   └── <namespace>/
│       ├── kustomization.yaml   # Lists apps + components for the namespace (alphabetical)
│       ├── namespace.yaml
│       └── <app>/
│           ├── ks.yaml          # Flux Kustomization (entry point)
│           └── app/
│               ├── ocirepository.yaml   # Per-app chart source (OCI; preferred)
│               ├── helmrelease.yaml
│               ├── externalsecret.yaml  # optional
│               ├── httproute.yaml       # optional (inline route: in HR values is preferred)
│               └── kustomization.yaml
├── components/            # Reusable Kustomize components (global-vars, alerts, volsync, gatus, …)
└── flux/                  # Core Flux bootstrap config (cluster/ks.yaml = root Kustomization)
bootstrap/                 # Cluster bootstrap (just tasks + helmfile)
talos/                     # Talos OS machine configs (talconfig.yaml + patches)
.agents/                   # Tool-agnostic agent instructions + skills (see "Agent tooling" below)
```

## Key technologies

| Category | Tool                         | Purpose                                     |
| -------- | ---------------------------- | ------------------------------------------- |
| OS       | Talos Linux (immutable)      | Node OS; pins kubelet + node together       |
| GitOps   | Flux                         | Deploys configs from Git to Kubernetes      |
| CI       | Renovate + GitHub Actions    | Dependency updates, validation              |
| CNI      | Cilium                       | Pod networking                              |
| Ingress  | Envoy Gateway (Gateway API)  | L7 routing via `HTTPRoute` (not Ingress)    |
| DNS      | external-dns + cloudflared   | Internal (OPNsense) + external (Cloudflare) |
| Secrets  | external-secrets + 1Password | Secret management                           |
| Storage  | Rook-Ceph + OpenEBS          | Block + node-local volumes                  |
| Backups  | VolSync (Kopia) → NFS/remote | PVC snapshots and backups                   |
| Charts   | bjw-s `app-template`         | The chart most apps use                     |

## GitOps flow

```text
Git push → Flux detects change → reconciles Kustomizations → deploys HelmReleases
```

The top-level Kustomization (`kubernetes/flux/cluster/ks.yaml`) recursively discovers every app under
`kubernetes/apps/` and applies default patches to each child Kustomization, including
`postBuild.substituteFrom` injecting `cluster-secrets` (from 1Password via ExternalSecret) and the
HelmRelease install/upgrade/rollback strategy defaults.

## App conventions

Every app follows the same shape. The `ks.yaml` is the Flux entry point and uses YAML anchors
(`&app`, `&namespace`, `*app`) for DRY references; it sets `targetNamespace: *namespace`, and any
`components` (`gatus/guarded`, `volsync`, `alerts`) plus their `postBuild.substitute` values
(`APP: *app`, `VOLSYNC_CAPACITY`) live in `ks.yaml` — never duplicated into `app/kustomization.yaml`.

Inside `app/`:

- **Chart source is per-app.** Each app-template app has its own `app/ocirepository.yaml` pointing at
  `oci://ghcr.io/bjw-s-labs/helm/app-template`; the HelmRelease references it via
  `spec.chartRef.kind: OCIRepository`, `name: <app>`. There is **no** shared `app-template`
  OCIRepository. (Non-app-template charts may use a `HelmRepository` source instead.)
- **HelmRelease `spec` order** is `interval` → `chartRef` → `dependsOn` → `values` (this repository orders
  `interval` before `chartRef`; most HRs omit `install`/`upgrade` and inherit them from the root
  Kustomization).
- **Routing** is usually an inline `route:` in HR values on the `envoy-internal` / `envoy-external`
  listeners (namespace `network`); a standalone `httproute.yaml` is the rarer case. Hosts are
  `${APP}.${SECRET_DOMAIN}` (and `${SECRET_INTERNAL_DOMAIN}` for internal).
- **App names avoid hyphens so the host stays clean.** The route host follows
  `{{ .Release.Name }}.${SECRET_DOMAIN}`, so a hyphen in the app name leaks into the URL. Name new
  apps hyphen-free end-to-end (directory, `ks.yaml` `&app`, HelmRelease, controller, PVC) — e.g.
  `reactiveresume`, not `reactive-resume` — and keep the standard `{{ .Release.Name }}` host instead
  of hardcoding a stripped literal. External identifiers a rename would churn (the 1Password item,
  the database name, S3 bucket) can stay as-is. Existing hyphenated apps predate this and are left
  alone.

### Secrets

Flow is **1Password → ExternalSecret → Kubernetes Secret**. Per-app `externalsecret.yaml` files use
`secretStoreRef.kind: ClusterSecretStore`, `name: onepassword-connect` (reads the `Talos` 1Password
vault). Apps with an ExternalSecret should `dependsOn` `onepassword-connect` in
`external-secrets`. Never commit plain-text secrets.

### House rules

- Namespace `kustomization.yaml` lists apps **alphabetically** and references the namespace's
  components (typically `global-vars` + `alerts`).
- `ConfigMap` resources must set `metadata.namespace` explicitly (Checkov CKV_K8S_21 fails the
  `default` namespace).
- Flux `postBuild` replaces `${VAR}` against `cluster-secrets`; **undefined vars become empty
  strings**. Any literal `${VAR}` you want preserved (Grafana dashboards, envsubst templates, shell
  snippets) must be escaped as `$${VAR}`.
- GPU workloads use `runtimeClassName: nvidia`.

## Validation

CI validates PRs with [flate](https://github.com/home-operations/flate) (HelmRelease + Kustomization
testing without a live cluster), plus security scans (Checkov/Trivy) and super-linter. Mirror locally
before pushing:

```bash
# Render a single app's HelmRelease
flate build hr <app> -n <namespace> --path kubernetes/flux/cluster

# Test all Kustomizations + HelmReleases
flate test all --path kubernetes/flux/cluster --allow-missing-secrets
```

## Agent tooling

Tool-agnostic agent instructions and skills live under `.agents/`:

- **`.agents/instructions/sorting.instructions.md`** — YAML sorting conventions (alphabetical
  defaults + app-template-specific ordering). Apply when asked to sort YAML.
- **`.agents/skills/add-app/`** — a skill that scaffolds a new app-template application following the
  conventions above. (Claude Code discovers it via a local `.claude/skills/add-app` symlink.)
