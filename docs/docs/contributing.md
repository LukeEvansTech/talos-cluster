# Contributing

This is a GitOps monorepo: declare what you want in Git, open a pull request, and let Flux reconcile
it. Start with the [Architecture overview](architecture/overview.md) for the big picture, then follow
the conventions below.

## Agent tooling

Tool-agnostic agent instructions and skills live under `.agents/` so any assistant (Codex, Copilot,
Cursor, Claude Code) can apply the same conventions:

- `.agents/instructions/sorting.instructions.md` — YAML sorting conventions (alphabetical defaults
  plus app-template-specific ordering). Apply this when asked to sort YAML.
- `.agents/skills/add-app/SKILL.md` — a skill that scaffolds a new app-template application
  following the conventions below. Agent tools that read `.agents/skills/` can invoke it directly.

`AGENTS.md` at the repository root is the canonical conventions guide. The local `CLAUDE.md` is a
standalone file and does not import `AGENTS.md`.

## Adding a new app

Use the `add-app` skill to scaffold, or follow the shape by hand. Every app lives at
`kubernetes/apps/<namespace>/<app>/` and follows the same pattern:

- **`ks.yaml`** is the Flux entry point:
  - Use YAML anchors (`&app`, `&namespace`, `*app`) for DRY references and set
    `targetNamespace: *namespace`.
  - Declare any `components` (`gatus/guarded`, `volsync`, `alerts`) here, along with their
    `postBuild.substitute` values (`APP: *app`, `VOLSYNC_CAPACITY`). Do not duplicate components into
    `app/kustomization.yaml`.
  - Add `dependsOn` `onepassword-connect` in `external-secrets` if the app uses an ExternalSecret.
- **Inside `app/`**:
  - Add a **per-app `ocirepository.yaml`** pointing at `oci://ghcr.io/bjw-s-labs/helm/app-template`;
    the HelmRelease references it via `spec.chartRef.kind: OCIRepository`, `name: <app>`. There is no
    shared `app-template` source. Non-app-template charts may use a `HelmRepository` instead.
  - Order the HelmRelease `spec` as `interval` → `chartRef` → `dependsOn` → `values`; most HRs omit
    `install`/`upgrade` and inherit them from the root Kustomization.
  - Prefer an inline `route:` in the HelmRelease values on the `envoy-internal` / `envoy-external`
    listener (namespace `network`) over a standalone `httproute.yaml`. Hosts are
    `${APP}.${SECRET_DOMAIN}` and `${APP}.${SECRET_INTERNAL_DOMAIN}`.
  - Add a per-app `externalsecret.yaml` if it needs secrets (see [Secrets](architecture/secrets.md)).
- **Register the app** in the namespace's `kustomization.yaml`, keeping the list **alphabetical**, and
  reference the namespace's components (typically `global-vars` + `alerts`).

### House rules to respect

- `ConfigMap` resources must set `metadata.namespace` explicitly — Checkov (CKV_K8S_21) scans raw YAML
  before Flux applies `targetNamespace` and flags `default`.
- Escape any literal `${VAR}` you want preserved as `$${VAR}`; Flux substitutes unescaped `${VAR}`
  against `cluster-secrets`/`cluster-settings`, and undefined vars become empty strings.
- GPU workloads set `runtimeClassName: nvidia`.
- Keep the repository public-safe: no LAN IPs, node or device hostnames, MACs, or internal hostnames
  in Git. The security-scans CI guard enforces this. See [Secrets](architecture/secrets.md).

## Validate before pushing

CI validates pull requests with flate (HelmRelease + Kustomization rendering without a live cluster),
plus security scans (Checkov/Trivy) and super-linter. Mirror it locally first:

```bash
# Render a single app's HelmRelease
flate build hr <app> -n <namespace> --path kubernetes/flux/cluster --allow-missing-secrets

# Test all Kustomizations + HelmReleases
flate test all --path kubernetes/flux/cluster --allow-missing-secrets
```

## Where AI planning artifacts go

AI planning artifacts (specs, plans, scratch notes from superpowers-style workflows) live in the
gitignored `docs/superpowers/` directory — they are not committed. Durable knowledge that future
maintainers and agents need is promoted into this knowledge base instead, so the published site stays
the single source of truth.
