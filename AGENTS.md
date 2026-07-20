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
├── components/            # Reusable Kustomize components (global-vars, alerts, volsync, homepage, kopiur, …)
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
| Storage  | Rook-Ceph + miroir           | Block + node-local volumes                  |
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
`components` (`volsync`, `alerts`, `homepage`, `kopiur`) plus their `postBuild.substitute` values
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
  `${APP}.${SECRET_DOMAIN}` — one hostname per route, whichever gateway it attaches to. Do not add a
  `${SECRET_INTERNAL_DOMAIN}` alias: it resolves to the same gateway as the primary domain, so it
  buys no extra restriction, and each alias costs an OPNsense host-override record. The record count
  has a hard ceiling (~421) above which external-dns silently stops publishing anything cluster-wide
  (see `docs/docs/architecture/split-dns.md`).
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
- **Gatus monitoring is automatic** — the gatus-sidecar chart watches HTTPRoutes cluster-wide, so a
  new app's route gets an uptime check with no per-app config (the old `gatus/guarded` component is
  gone). Opt a route out with a `gatus.home-operations.com/enabled: "false"` annotation; opt a
  Service in with `"true"` plus an optional `gatus.home-operations.com/endpoint:` YAML block for
  name/group overrides.
- GPU workloads use `runtimeClassName: nvidia`.

## Provisioning a new app

The `.agents/skills/add-app` skill scaffolds the four manifests; the cluster-specific work is the
out-of-band prerequisites and the validation the skill cannot do.

- **Secrets and external stores are provisioned outside Git, then referenced by an ExternalSecret.**
  Create the item in the `Talos` 1Password vault first
  (`op item create --vault Talos --category "API Credential" --title <app> "FIELD[password]=…"`);
  the app's `externalsecret.yaml` then `extract`s it. Generate values with `openssl rand -hex 32` and
  never echo them.
- **Each shared data service needs its own step** — one top-level item per service:
- **CNPG Postgres** — add a `ghcr.io/home-operations/postgres-init` initContainer (`envFrom` the
  app secret, `INIT_POSTGRES_*`) to create the database and role; mirror `paperless`. Connect with
  `sslmode=require`. Node / `pg` apps additionally need `NODE_TLS_REJECT_UNAUTHORIZED=0` — the
  bundled driver verifies the cert-manager CA it cannot reach from the app namespace.
- **Dragonfly (Redis)** — authenticated; template
  `redis://default:{{ .DRAGONFLY_PASSWORD }}@dragonfly.database.svc.cluster.local:6379` from the
  `dragonfly` item. A client without the password fails silently at runtime.
- **Garage (S3)** — provision a bucket and access key with the `/garage` CLI inside `garage-0`
  (`storage` namespace), store the key in the `garage` item, and point the app at
  `http://garage.storage.svc.cluster.local:3900` (region `us-east-1`, path-style).
- **Anchored ports are unquoted integers** (`PORT: &port 3000`). A quoted `"3000"` reused for a
  probe `httpGet.port` is rejected at apply time ("must contain at least one letter").
- **Validate** with `kustomize build <appDir>` and flate, but note they check the HelmRelease and
  Kustomization, not the rendered Deployment — API-level errors surface only when Flux applies. If a
  first deploy fails, read the HelmRelease `status` for the apply error.
- Provisioning uses biometric `op` and `kubectl` / `/garage` with the sandbox disabled.

## Archiving an app

Both methods rely on `prune: true`: removing an app from the namespace `kustomization.yaml` makes
Flux delete its live HelmRelease, PVC, and everything else it owns.

- **Archive (permanent):** `git mv kubernetes/apps/<ns>/<app> .archive/kubernetes/apps/<ns>/<app>`
  and delete its `./<app>/ks.yaml` line from the namespace `kustomization.yaml`. `.archive/` is a
  top-level directory outside the Flux-watched `kubernetes/` tree, so the manifests are kept for
  reference but never reconciled. Also drop any homepage tile and `dependsOn` references.
- **Disable in place (temporary):** comment out the `# - ./<app>/ks.yaml` line with a reason
  (`# Disabled — using X instead`). Quick to re-enable, and Flux still prunes the live resources.

Either way Flux **prunes the PVC** — take a VolSync snapshot or copy the data out
(`just kube browse-pvc`) first if it matters.

## Validation

PR renders and diffs are posted by the in-cluster **Konflate** as a native `Konflate` commit status
plus a PR comment — there is no GitHub Actions render workflow. GitHub Actions still run security
scans (Checkov/Trivy → Code Scanning) and super-linter. Mirror the render locally before pushing
with [flate](https://github.com/home-operations/flate) (in the mise toolchain), via the just
wrappers:

```bash
# Render a single app's HelmRelease / Kustomization
just kube flate-build-hr <namespace> <app>
just kube flate-build-ks <namespace> <app>

# Test all Kustomizations + HelmReleases (the full CI-equivalent check)
just kube flate-test
```

## Agent tooling

Tool-agnostic agent instructions and skills live under `.agents/`:

- **`.agents/instructions/sorting.instructions.md`** — YAML sorting conventions (alphabetical
  defaults + app-template-specific ordering). Apply when asked to sort YAML.
- **`.agents/skills/add-app/`** — a skill that scaffolds a new app-template application following the
  conventions above. (Claude Code discovers it via a local `.claude/skills/add-app` symlink.)
