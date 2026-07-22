# Renovate auto-merge policy

[Renovate](https://docs.renovatebot.com/) raises dependency-update PRs across the cluster. The
auto-merge policy follows a **denylist** model: application updates auto-merge by default, while a
protected set of cluster-critical infrastructure stays on manual review. The whole policy lives in
`.renovaterc.json5`.

## How it works

Renovate evaluates `packageRules` top to bottom, **last match wins per field**. Three rules implement
the policy, in this order:

1. A broad **catch-all for minor/patch** enables auto-merge for every container image and Helm
   chart, gated by a cooldown.
2. A second broad **catch-all for digest + pins** enables auto-merge for the same datasources with
   no cooldown.
3. A **protected-infra guard**, placed immediately after both, sets `automerge: false` again for the
   cluster-critical components — so it overrides both catch-alls for those packages only.

`major` updates are never listed in any rule, so they always open a PR and wait for a human.

## What auto-merges

Application container images and Helm charts auto-merge under **two** rules, split by update type:

```json5
{
  description: "Auto-merge apps — minor/patch (denylist model; protected guard below)",
  matchDatasources: ["docker", "helm"],
  matchUpdateTypes: ["minor", "patch"],
  automerge: true,
  automergeType: "pr",
  platformAutomerge: false,
  ignoreTests: false,
  minimumReleaseAge: "2 days",
},
{
  description: "Auto-merge apps — digest + pins, no age gate (denylist model; protected guard below)",
  matchDatasources: ["docker", "helm"],
  matchUpdateTypes: ["digest", "pin", "pinDigest"],
  automerge: true,
  automergeType: "pr",
  platformAutomerge: false,
  ignoreTests: false,
}
```

- `automergeType: "pr"` opens a normal PR so CI runs against the change.
- `ignoreTests: false` together with `platformAutomerge: false` mean **Renovate self-gates on CI** —
  it holds the merge until the PR's checks are green (the `Lint` and `security-scans` GitHub Actions
  workflows, the Konflate commit status, and the `claude/renovate-review` commit status — see
  [AI review of Renovate PRs](#ai-review-of-renovate-prs) below), rather than relying on GitHub-native
  auto-merge (the repo has no required-status-check rulesets).
- `minimumReleaseAge: "2 days"` on the **minor/patch** rule is a cooldown: a release must be two days
  old before it can merge, giving yanked tags, broken `.0` releases, and runtime regressions a window
  to surface first.
- The **digest/pin/pinDigest** rule deliberately has **no** `minimumReleaseAge` (removed by #3608).
  Digest bumps track mutable tags that upstream rebuilds every few days (e.g. `nginx:1.31-alpine`,
  `llama.cpp` `server-cuda`), so a fixed age gate perpetually resets and never clears — PRs sat
  pending for up to 19 days before the fix. GHCR also often has no retrievable per-digest timestamp
  at all, which pends forever too. Digest bumps stay CI-gated (`ignoreTests: false`) like everything
  else, and the protected-infra guard still applies to them.

## What stays manual

The protected-infra guard keeps these on manual review for all update types (including `digest`):

| Area | Packages (substring match in `matchPackageNames`) |
| --- | --- |
| OS / kubelet | `siderolabs/` (Talos), `kubelet` |
| Control plane | `kube-apiserver`, `kube-controller-manager`, `kube-proxy`, `kube-scheduler` |
| CNI | `cilium` |
| Storage | `rook-ceph`, `rook-ceph-cluster`, `miroir` |
| Node upgrades | `tuppr` (drives Talos + Kubernetes node upgrades) |
| Backups | `volsync` (backup mover), `snapshot-controller` (CSI VolumeSnapshot controller) |
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

- **Cooldown** — `minimumReleaseAge: "2 days"` on the minor/patch app rule only; the digest/pin/
  pinDigest rule has no cooldown at all (removed by #3608, see [What
  auto-merges](#what-auto-merges) above).
- **CI gate** — each app merge waits for the `Lint` and `security-scans` GitHub Actions workflows,
  the Konflate commit status, and the `claude/renovate-review` commit status to pass before Renovate
  merges it.
- **Kill switch** — set **both** app catch-all rules' `automerge: false` (the minor/patch rule and the
  digest/pin/pinDigest rule) to pause *all* app auto-merge and fall back to hand-merging, without
  touching the protected list. Flipping only one still leaves the other update-type category
  auto-merging.

## Maintaining the protected set

To move a component between auto-merge and manual review:

- **Protect a new component** — add a substring pattern to the guard rule's `matchPackageNames`.
- **Unprotect** — remove its pattern; it then auto-merges under whichever catch-all rule matches its
  update type.

When adding a pattern, confirm it does not accidentally match an unrelated application image (the
patterns are unanchored substrings).

## Operational tuning: notifications, rebasing, review

Three pieces of operational hygiene sit alongside the auto-merge policy. They are independent of it,
but they shape how Renovate behaves day to day.

### Actionable-only email notifications

The goal is for Renovate to email **only** the PRs that need a human. The mechanism:

- `.renovaterc.json5` sets `assignees: ["LukeEvansTech"]` with `assignAutomerge: false` (the
  default, pinned). Manual PRs (the protected-infra guard + any `major`) get assigned at creation,
  which reaches the *Participating* notification stream; clean auto-merge PRs stay unassigned and
  silent.
- `.github/workflows/renovate-assign-on-failure.yaml` runs on the `security-scans` / `Lint`
  `workflow_run` (there is no Flate GitHub Actions check anymore — it was deleted in #3375; render
  validation moved to the in-cluster Konflate, which posts its own commit status). When a check
  **fails** on a Renovate PR, it assigns the PR — a failed check means Renovate won't merge it (it
  self-gates on CI), so the PR needs a human.

> **Load-bearing non-git dependency:** the quiet inbox also depends on the repository's GitHub
> **Watch** level being **"Participating and @mentions"** (not "All Activity"). Assignment reaches
> the *Participating* stream regardless of watch level; the quiet watch level is what suppresses the
> all-activity firehose. If the Renovate email flood ever returns, check that the Watch setting
> wasn't reset to "All Activity".

### Avoid the rebase / CI re-run storm

Leave `rebaseWhen: "conflicted"` set at the top level of `.renovaterc.json5`. The diagnostic
signature of getting this wrong is open Renovate PRs piling up dozens of CI re-runs and a matching
wall of `github-actions` comments. The cause: with `automerge: true`, an unset `rebaseWhen` inherits
`behind-base-branch`, so **every merge to `main` re-rebases every open PR and re-runs the full CI
suite** (N open PRs × M merges/day of wasted runs). `conflicted` rebases only on a genuine textual
conflict — ~80–95 % fewer runs. It's safe here because `platformAutomerge: false` self-merges via
the Renovate API and there's no branch-protection ruleset requiring up-to-date branches, so a
behind-base-but-green PR merges fine. (The fix is **not** pruning workflows — the per-PR suite is
each justified; the waste was re-run *frequency*.)

### AI review of Renovate PRs

`.github/workflows/renovate-review.yaml` reviews Renovate PRs with `anthropics/claude-code-action`
(Claude via OAuth, sidestepping the internal-only LiteLLM). It is gated to `renovate[bot]` PRs,
skips digest-only and github-action bumps, and tiers the model — a cheaper model for routine patch
container bumps, a stronger one for minor/major/chart or high-blast-radius components. It posts a
`claude/renovate-review` commit status that gates auto-merge via all-checks-green.

The verdict is deliberately **fail-open**: `APPROVED` → success, `CHANGES_REQUESTED` → failure
(blocks), but if the Claude step itself *errors* (API blip, rate-limit, timeout) it passes through
so a transient blip doesn't wedge auto-merge.

> **Rotate the OAuth token before it lapses.** The `CLAUDE_CODE_OAUTH_TOKEN` secret has a **1-year,
> non-refreshable** lifetime (generated 2026-06-02 → **expires 2027-06-02**, tracked on the
> `Talos` 1Password item). Because errored reviews pass through, a **lapsed token lets PRs
> auto-merge unreviewed**. Rotate with `claude setup-token`, then
> `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R LukeEvansTech/talos-cluster`, and bump the 1Password
> item's expiry.

## Why not adoption-based confidence?

Renovate's [Merge Confidence](https://docs.renovatebot.com/merge-confidence/) can gate auto-merge on
an **Adoption** score — the percentage of a package's Renovate users already on the new release — via
`matchConfidence`. It only covers seven language ecosystems (Go, JavaScript, Java, Python, .NET, PHP,
Ruby), **not Docker images or Helm charts**, which is effectively the entire cluster. So
`minimumReleaseAge` is the deliberate time-based substitute for versioned (minor/patch) updates:
"survived two days in the wild" stands in for "widely adopted". Digest/pin updates get no such
substitute — see [What auto-merges](#what-auto-merges) for why a cooldown doesn't work for them.
