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
>
> **This covers prose, not just files.** Commit messages and pull request bodies are equally
> world-visible, and a squash merge copies the PR body verbatim into the commit message on `main`,
> where it is permanent — that is how a LAN IP and the internal zone name reached history in #3803.
> Write the _why_ without the coordinates: `${SECRET_DOMAIN}` rather than the real name, "the NAS
> endpoint" rather than its address, "a delegated internal zone" rather than the directory-service
> rebuild that caused it. Do not append AI session links or co-author trailers.
> `.github/scripts/check_internal_identifiers.py --text-file` enforces this at `commit-msg` via
> lefthook and on PR title/body via `.github/workflows/pr-hygiene.yml`, reusing the same patterns as
> the tracked-file scan so prose and files cannot drift apart. The internal-zone regex is fed in
> at runtime via `INTERNAL_DOMAIN_RE` (a repository secret in CI, a gitignored `.mise.local.toml`
> locally) rather than hardcoded, since the script itself is public.

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

## Code Review Rules

Read by Codex code review, and by any other reviewer that honours this file. A Renovate-facing
reviewer already runs here: the `claude/renovate-review` commit status is the **gate** on dependency
PRs (upstream changelog research, breaking-change verdict, required check on `main`). Reviews driven
by this section are **advisory** — spend them on how the diff wires into _this_ repository rather than on
re-deriving upstream release notes.

Flag consequential, repository-specific breakage. Prefer silence over style commentary.

### Always flag

- **Internal coordinates in a public repository.** LAN IPs, `.lan` / `.internal` hostnames, device or node
  names, deployment topology, MACs, disk serials — in files, and equally in the PR title and body,
  since a squash merge copies the body into `main` permanently. Safe path: the `${SECRET_DOMAIN}` /
  `${SECRET_INTERNAL_DOMAIN}` placeholders, or a real address templated inside an `ExternalSecret`'s
  `target.template.data` and mounted from the rendered Secret — never a `ConfigMap` in Git.
- **Plaintext secrets.** Every secret arrives via 1Password → ExternalSecret. A literal token,
  password, or key in a manifest is a blocker regardless of how narrowly scoped it looks.
- **A literal `${VAR}` left unescaped.** Flux `postBuild` substitutes `${VAR}` against
  `cluster-secrets` and **undefined vars become empty strings**. A Grafana dashboard, envsubst
  template, or shell snippet that needs the literal must escape it as `$${VAR}`. This fails silently
  — the config deploys blank instead of erroring.
- **An app removal that prunes data.** The root Kustomization sets `prune: true`, so deleting an
  app's line from the namespace `kustomization.yaml` makes Flux delete its PVC. Flag unless the PR
  shows a VolSync snapshot or copy-out happened first.

### Worth flagging

- A `ConfigMap` with no explicit `metadata.namespace` — Checkov CKV_K8S_21 fails the `default`
  namespace.
- A quoted anchored port (`PORT: &port "3000"`) reused as a probe `httpGet.port`. Rejected at apply
  time with "must contain at least one letter"; ports are unquoted integers.
- A new app whose name contains a hyphen. The route host is `{{ .Release.Name }}.${SECRET_DOMAIN}`,
  so the hyphen leaks into the URL — name new apps hyphen-free end-to-end. Existing hyphenated apps
  predate the rule and are left alone.
- A second route hostname, or a `${SECRET_INTERNAL_DOMAIN}` alias beside the primary domain. It
  resolves to the same gateway so it buys no extra restriction, and each alias costs an OPNsense
  host-override record against a hard ceiling (~421) above which external-dns silently stops
  publishing anything cluster-wide.
- An app with an `externalsecret.yaml` that does not `dependsOn` `onepassword-connect` in
  `external-secrets`.
- Components (`volsync`, `alerts`, `homepage`, `kopiur`) or their `postBuild.substitute` values
  declared in `app/kustomization.yaml` instead of `ks.yaml`.
- A bump to cluster-critical infrastructure — the "Protected infra" `packageRules` entry in
  `.renovaterc.json5` is the source of truth (Cilium, Rook-Ceph, Flux, Talos, cert-manager,
  external-secrets, Envoy Gateway, CloudNativePG, …) — that needs a repo-side change the PR does not
  make: a renamed Helm value, a CRD version the manifests still pin, a required migration step.
- A GPU workload missing `runtimeClassName: nvidia`.

### Expected patterns — do not flag

- Bare `${VAR}` in manifests. That is the Flux substitution mechanism, not an undefined variable.
- YAML anchors (`&app`, `&namespace`, `*app`) in `ks.yaml` — the house DRY pattern, not duplication.
- Digest-pinned container tags (`tag@sha256:…`). Renovate owns them; do not suggest stripping them.
- Flux `OCIRepository` tags that are **not** digest-pinned. Helm rejects an appended digest, so the
  carve-out is deliberate rather than drift from the digest-pinning convention.
- `HTTPRoute` with no `Ingress` anywhere. Routing is Envoy Gateway + Gateway API by design.
- Schema or kubeconform-style complaints about raw YAML. The source is meaningless until kustomize
  substitutes vars and merges components; real validation is `just kube flate-test`.
- Secrets referenced but absent from Git (the ExternalSecret is the mechanism), and anything under
  `.archive/` (kept for reference, never reconciled).
- Missing PDBs, extra replicas, or other production-grade HA asks. This is one homelab cluster of
  three control-plane nodes with no separate workers.
- HelmRelease `spec` key order, formatting, and line width — super-linter, prettier, and yamlfmt own
  those.
- In-cluster service DNS names (`<svc>.<ns>.svc.cluster.local`). These are published by
  construction: the app's whole directory is in this repository, so the name is derivable from the
  tree and this file documents several of them itself. The public-repository rule targets LAN
  addresses, `.lan` / `.internal` hostnames, and device names — not names that only resolve inside
  the cluster.
- Scale and count details ("about N sources share one repository", request rates, retention
  counts). Operational magnitude is not deployment topology; what the rule prohibits is
  coordinates — an address, a hostname, a device name — not how much of something there is.
- A hyphen in the name of a **CR-only** app: one whose `app/` holds custom resources with no
  HelmRelease, controller, or route. The hyphen-free rule exists so the route host
  (`{{ .Release.Name }}.${SECRET_DOMAIN}`) stays clean, and an app with no Helm release has no such
  host. `truenas-exporter` and `nut-appliance` are the shape to compare against, and most of the
  `observability` namespace follows them.

## Agent tooling

Tool-agnostic agent instructions and skills live under `.agents/`:

- **`.agents/instructions/sorting.instructions.md`** — YAML sorting conventions (alphabetical
  defaults + app-template-specific ordering). Apply when asked to sort YAML.
- **`.agents/skills/add-app/`** — a skill that scaffolds a new app-template application following the
  conventions above. (Claude Code discovers it via a local `.claude/skills/add-app` symlink.)
