# Port joryirving/home-ops `.agents/` to talos-cluster

- **Date:** 2026-06-15
- **Status:** Design approved (brainstorming), pending spec review
- **Source:** <https://github.com/joryirving/home-ops/tree/main/.agents>
- **Branch / worktree:** `worktree-feat+agents-port` at `.claude/worktrees/feat+agents-port`

## Goal

Adapt the agent-tooling content from `joryirving/home-ops/.agents` to this cluster's
conventions, and surface the structural differences so they can be evaluated. This is a
port + adaptation, not a layout migration — none of joryirving's repo structure (multi-cluster
base+overlay) is being adopted.

## Source inventory

joryirving's `.agents/` contains exactly three files plus a root `AGENTS.md` it leans on:

1. `.agents/instructions/pr-review.instructions.md` — system prompt for a `misospace/pr-reviewer-action` GitHub workflow.
2. `.agents/instructions/sorting.instructions.md` — YAML sorting conventions (generic + app-template-specific).
3. `.agents/skills/add-app/SKILL.md` — scaffold a new app-template application.

## Scope decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Wiring model | Canonical files under `.agents/`, symlinked into `.claude/` | Maximum portability (Copilot/Codex/Cursor) **and** Claude Code auto-discovery. |
| `pr-review.instructions.md` | **Skip** | It is the literal `system_prompt_file` for `misospace/pr-reviewer-action`, which this repo does not run (this repo uses `renovate-review.yaml` / claude-code-action). Its "documented conventions" are joryirving-specific and partly contradict this repo (we *do* set `metadata.namespace` on ConfigMaps for Checkov CKV_K8S_21; we *do* use per-app `ocirepository.yaml`). |
| `sorting.instructions.md` | **Port** (light adaptation) | Generic YAML + app-template rules; this repo is an app-template shop. |
| `add-app/SKILL.md` | **Port** (heavy adaptation) | High value; needs full remap to this repo's single-cluster, per-app `app/` layout. |
| Root `AGENTS.md` | **Public-safe committed file** (revised — see note) | Originally planned as a `→ CLAUDE.md` symlink, but `CLAUDE.md` is **gitignored/private** and contains LAN IPs, so a symlink would dangle in clones and risk leaking internal data. Instead AGENTS.md is a standalone public-safe conventions guide (scrubbed of IPs/hostnames/device names); Claude Code auto-loads it via an `@AGENTS.md` import in the local private `CLAUDE.md`. |
| Process | Spec doc → review → plan → build, in a worktree | User preference. |

> **Discovered constraint (during build):** the repo's root `.gitignore` ignores **both `.claude/` and `CLAUDE.md`** — they are local/private (CLAUDE.md holds LAN IPs). Consequences: (a) the `.claude/skills/add-app` symlink is a **local-only** convenience (gitignored, per working tree), not a committed artifact; (b) AGENTS.md is a committed public file rather than a symlink; (c) the "Claude finds it automatically" wiring is a one-line `@AGENTS.md` import the user adds to their local `CLAUDE.md` post-merge. Separately, a **repo-wide exposure** of LAN IPs / node names / device models in tracked manifests and docs was surfaced and **deferred** by the user to a separate effort.

## Target layout & wiring

```
.agents/
├── instructions/
│   └── sorting.instructions.md         # adapted (light)
└── skills/
    └── add-app/
        └── SKILL.md                    # adapted (heavy)
AGENTS.md                                                   # COMMITTED public-safe conventions guide
.claude/skills/add-app  ->  ../../.agents/skills/add-app   # LOCAL-ONLY symlink (.claude is gitignored)
CLAUDE.md  (local/private)                                  # + `@AGENTS.md` import line (post-merge, local)
```

- `AGENTS.md` is a **committed public file** (not a symlink) — public-safe, scrubbed of IPs/hostnames/device names.
- The `add-app` skill is an action skill → exposed to Claude Code via a **relative directory
  symlink** `.claude/skills/add-app -> ../../.agents/skills/add-app`. Because `.claude/` is gitignored,
  this symlink is **local-only** (recreate it per working tree / per clone); it is *not* committed.
- `sorting.instructions.md` is *reference* material (not an action skill). It is committed under
  `.agents/instructions/` and referenced from `AGENTS.md`'s "Agent tooling" section.
- "Claude finds AGENTS.md automatically" is achieved by adding `@AGENTS.md` to the local (private)
  `CLAUDE.md`; Claude Code has no native AGENTS.md auto-load. This is a **post-merge local step**
  (CLAUDE.md is gitignored and absent from this worktree).

## Component 1 — `sorting.instructions.md` (light adaptation)

Ports near-verbatim. The single substantive change:

- **app-template detection rule.** joryirving detects app-template via `spec.chartRef.name: app-template`
  (one shared chart). Here, `chartRef.name` is the **app's own name** pointing at a **per-app
  `app/ocirepository.yaml`**. Rewrite the detection to: *"the app's `app/ocirepository.yaml` `url`
  ends in `bjw-s-labs/helm/app-template`"* (equivalently, `chartRef.kind: OCIRepository` referencing
  that per-app source).

Everything else maps cleanly and is retained unchanged:
- Alphabetical-by-default at every level; leading `---`; `yaml-language-server` schema comment.
- K8s ordering `apiVersion` → `kind` → `metadata` → `spec`.
- `metadata` ordering `name` → `namespace` → `annotations` → `labels`.
- app-template `spec` ordering — **corrected to this repo's actual convention** `interval` →
  `chartRef` → `dependsOn` → `install` → `upgrade` → `values` (this repo puts `interval` before
  `chartRef`, the reverse of joryirving; verified across `error-pages`, `mosquitto`, `karakeep`),
  `spec.values` (`defaultPodOptions` first, then alphabetical), and the
  controllers / containers / persistence / service nested ordering rules.
- The "do not sort YAML embedded inside string fields (e.g. `configMap.data.*`)" caveat.

## Component 2 — `add-app/SKILL.md` (heavy adaptation)

The skill must emit **this repo's** exact layout. Full remapping from joryirving's assumptions:

| joryirving assumption | this repo (adaptation) |
|---|---|
| base + overlay: `apps/base/<ns>/<app>/` + `apps/<cluster>/<ns>/<app>.yaml` | single tree: `apps/<ns>/<app>/ks.yaml` + `apps/<ns>/<app>/app/{ocirepository,helmrelease,kustomization,…}.yaml` |
| shared `chartRef.name: app-template`, **no** per-app ocirepository | **per-app `app/ocirepository.yaml`** (`oci://ghcr.io/bjw-s-labs/helm/app-template`, own `ref.tag`) + HR `chartRef.kind: OCIRepository, name: <app>` |
| overlay KS: name-anchor only, `postBuild.substitute {APP, CLUSTER}`, `wait: false`, namespace via `replacements` component | this repo's `ks.yaml`: `&app` + `&namespace` anchors, `commonMetadata.labels.app.kubernetes.io/name: *app`, `targetNamespace: *namespace`, `wait: true`, `retryInterval: 2m`, `timeout: 5m`; components (`gatus/guarded`, `volsync`, `alerts`) and substitutes (`APP: *app`, `VOLSYNC_CAPACITY`) go **in `ks.yaml`**, not a separate overlay file |
| `ClusterSecretStore name: onepassword` | **`onepassword-connect`** (reads the `Talos` 1Password vault); ES-using apps `dependsOn: { name: onepassword-connect, namespace: external-secrets }` |
| multi-cluster targeting question | **dropped** (single cluster) |
| route via inline values only | inline `route:` in HR values is the dominant pattern here (~117 inline vs ~12 separate `httproute.yaml`); skill prefers inline `route:`, offers separate `httproute.yaml` as the rarer option; hosts use `${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}`, listeners `envoy-internal` / `envoy-external` |
| `question` tool to gather inputs | **AskUserQuestion** |
| (n/a) | register app **alphabetically** in `apps/<ns>/kustomization.yaml`; ConfigMaps need explicit `metadata.namespace` (Checkov CKV_K8S_21); escape Flux-literal vars as `$${VAR}`; GPU workloads use `runtimeClassName: nvidia` |
| verify via manual file checks | **verify with `flate build hr <app> -n <ns> --path kubernetes/flux/cluster` and `flate test all --path kubernetes/flux/cluster --allow-missing-secrets`** |

Shared, no change needed: the schema host `k8s-schemas.home-operations.com` (this repo already uses it
in `ks.yaml`), and the bjw-s `app-template` chart itself.

### Files the adapted skill scaffolds

For app `<app>` in namespace `<ns>`:

- `kubernetes/apps/<ns>/<app>/ks.yaml` (Flux Kustomization; anchors, components, substitutes)
- `kubernetes/apps/<ns>/<app>/app/kustomization.yaml` (lists `ocirepository.yaml`, `helmrelease.yaml`, + optional `externalsecret.yaml` / `httproute.yaml`)
- `kubernetes/apps/<ns>/<app>/app/ocirepository.yaml` (per-app app-template source)
- `kubernetes/apps/<ns>/<app>/app/helmrelease.yaml` (app-template HR; inline `route:` when ingress needed)
- `kubernetes/apps/<ns>/<app>/app/externalsecret.yaml` (only if secrets needed; `onepassword-connect`)
- update `kubernetes/apps/<ns>/kustomization.yaml` to register the app alphabetically

Skill workflow keeps joryirving's good bones: collect details → **inspect 1-2 neighbouring apps in the
same namespace and match their patterns** → scaffold → verify. Confirm before writing files.

## Worth flagging (NOT building now)

- **`misospace/pr-reviewer-action` workflow** (the action itself, separate from the skipped
  instructions file): reviews *all* PRs with a structured JSON verdict, evidence providers, and
  host-platform compatibility-matrix checks for version bumps — broader than this repo's
  Renovate-only `renovate-review.yaml`. Candidate future enhancement; flagged, not adopted.
- joryirving's **base+overlay multi-cluster layout** and **`replacements` component**: *not* worth
  adopting — single-cluster here; would be a large refactor for zero benefit.

## Verification

1. `.agents/instructions/sorting.instructions.md` and `.agents/skills/add-app/SKILL.md` exist and read correctly.
2. Symlinks resolve: `.claude/skills/add-app/SKILL.md` and `AGENTS.md` both readable through the link;
   `git ls-files -s` shows mode `120000` (symlink) for both.
3. `CLAUDE.md` has the sorting-instructions pointer line.
4. No secrets / LAN IPs / internal hostnames introduced (repo is public).
5. `markdownlint` / super-linter pass for the new markdown (mirror with `just lint` if needed).
