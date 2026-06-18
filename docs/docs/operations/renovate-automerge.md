# Renovate auto-merge policy

[Renovate](https://docs.renovatebot.com/) raises dependency-update PRs across the cluster. The
auto-merge policy follows a **denylist** model: application updates auto-merge by default, while a
protected set of cluster-critical infrastructure stays on manual review. The whole policy lives in
`.renovaterc.json5`.

## How it works

Renovate evaluates `packageRules` top to bottom, **last match wins per field**. Two rules implement
the policy, in this order:

1. A broad **catch-all** enables auto-merge for every container image and Helm chart.
2. A **protected-infra guard**, placed immediately after, sets `automerge: false` again for the
   cluster-critical components — so it overrides the catch-all for those packages only.

`major` updates are never listed in either rule, so they always open a PR and wait for a human.

## What auto-merges

Application container images and Helm charts auto-merge for `minor`, `patch`, and `digest` updates:

```json5
{
  description: "Auto-merge apps — minor/patch/digest (denylist model; protected guard below)",
  matchDatasources: ["docker", "helm"],
  matchUpdateTypes: ["minor", "patch", "digest"],
  automerge: true,
  automergeType: "pr",
  platformAutomerge: false,
  ignoreTests: false,
  minimumReleaseAge: "3 days",
}
```

- `automergeType: "pr"` opens a normal PR so CI runs against the change.
- `ignoreTests: false` together with `platformAutomerge: false` mean **Renovate self-gates on CI** —
  it holds the merge until the branch's checks (flate, security-scans) are green, rather than relying
  on GitHub-native auto-merge (the repo has no required-status-check rulesets).
- `minimumReleaseAge: "3 days"` is a cooldown: a release must be three days old before it can merge,
  giving yanked tags, broken `.0` releases, and runtime regressions a window to surface first.

## What stays manual

The protected-infra guard keeps these on manual review for all update types (including `digest`):

| Area | Packages (substring match in `matchPackageNames`) |
| --- | --- |
| OS / kubelet | `siderolabs/` (Talos), `kubelet` |
| Control plane | `kube-apiserver`, `kube-controller-manager`, `kube-proxy`, `kube-scheduler` |
| CNI | `cilium` |
| Storage | `rook-ceph`, `rook-ceph-cluster` |
| GitOps | `fluxcd/` (controllers), `controlplaneio-fluxcd` (operator + instance) |
| Certs / DNS / secrets | `cert-manager`, `coredns`, `external-secrets`, `1password` (onepassword-connect) |
| Image mirror | `spegel` |
| Database | `cloudnative-pg` (operator + `postgresql` image + Barman plugin) |
| GPU | `nvidia`, `dcgm` |
| Ingress | `envoyproxy` (Envoy Gateway + Envoy) |

Patterns are unanchored substrings, so a single entry (e.g. `cloudnative-pg`) covers the operator
chart, the `postgresql` image, and the Barman Cloud plugin at once.

The protected patterns intentionally overlap the `groups` rules (Cilium, Cert-Manager, …): the
`groups` block controls *grouping* of PRs, while the guard controls *auto-merge*. The two are
independent.

> **Note:** the `gateway-api` CRDs still auto-merge via a separate `github-releases` rule. Those
> bumps are additive and low-risk, so they are intentionally left on auto-merge even though Envoy
> Gateway itself is protected.

## Safety levers

- **Cooldown** — `minimumReleaseAge: "3 days"` on every app merge.
- **CI gate** — each app merge waits for flate + security-scans to pass before Renovate merges it.
- **Kill switch** — set the catch-all rule's `automerge: false` (one line) to pause *all* app
  auto-merge and fall back to hand-merging, without touching the protected list.

## Maintaining the protected set

To move a component between auto-merge and manual review:

- **Protect a new component** — add a substring pattern to the guard rule's `matchPackageNames`.
- **Unprotect** — remove its pattern; it then auto-merges under the catch-all.

When adding a pattern, confirm it does not accidentally match an unrelated application image (the
patterns are unanchored substrings).

## Why not adoption-based confidence?

Renovate's [Merge Confidence](https://docs.renovatebot.com/merge-confidence/) can gate auto-merge on
an **Adoption** score — the percentage of a package's Renovate users already on the new release — via
`matchConfidence`. It only covers seven language ecosystems (Go, JavaScript, Java, Python, .NET, PHP,
Ruby), **not Docker images or Helm charts**, which is effectively the entire cluster. So
`minimumReleaseAge` is the deliberate time-based substitute: "survived three days in the wild" stands
in for "widely adopted".
