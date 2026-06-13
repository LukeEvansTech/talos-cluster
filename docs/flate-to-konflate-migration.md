# Flate → Konflate Gate Migration

## Overview

This document is the design + runbook for replacing the CI-side `flate` render
gate with **konflate as the authoritative merge gate**, mirroring onedr0p's
home-ops architecture. Target completion: 2026-06.

Today the repo runs two render checks on every PR:

- `flate.yaml` — a GitHub Actions workflow running `flate test all` + `flate diff hr|ks` (~5 min/PR). This is the de-facto blocking gate.
- `konflate.yaml` — a GitHub Actions workflow that fetches the in-cluster konflate render summary and posts it as an advisory PR comment.

After this migration:

- **konflate posts a native `Konflate` commit status** (and PR summary comment) directly to each PR. That status is a **required branch-protection check** — the single render gate.
- **`flate.yaml` and `konflate.yaml` are deleted.** The `flate` binary survives only in `image-pull.yaml` (`flate get images` pre-pull), which is orthogonal.

## Motivation

- **Eliminate the flate v0.4.x cascade.** `flate test` builds the dependency DAG and, since v0.4.1–v0.4.3, cascades any *unresolvable* source to `blocked` for all dependents. The private `shelly-fleet` GitRepository (SSH key is runtime-only) is permanently unresolvable in CI, so it blocks ~209 items — forcing a pin to `flate v0.3.3`. konflate renders against the **live cluster**, where `shelly-fleet` is genuinely synced, so it marks that one source `skipped (missing secret)` and renders everything else (4 advisory failures, not 209 blocked). The pin problem disappears.
- **Faster PRs.** No ~5-min `flate test`/`diff` per PR; konflate renders out-of-band and the check is a sub-second status read.
- **Single source of truth.** One render (konflate) instead of two (flate CI + konflate CI), eliminating the duplicate diff comment.
- **Proven pattern.** onedr0p runs exactly this: no `flate.yaml`, konflate posts the `Konflate` commit status as the required check.

## Decisions (agreed)

| Decision | Choice |
|---|---|
| Gate mechanism | konflate **native commit-status write-back** (`statusChecks: true`), not a CI job |
| Gate strictness | `Konflate` is a **required** branch-protection check (accept konflate as a SPOF; mitigate with health alerting) |
| Auth | dedicated **GitHub App** (`konflate-bot`), client-id + private-key — not a PAT |
| Branch-protection scope | require **only** `Konflate` (lint/security/image-pull stay informational) |
| Renovate gating | manifest-affecting updates wait on `Konflate` (`automergeType: pr` + `ignoreTests: false`); non-manifest stay `branch`/`ignoreTests: true` |

## Current state (verified 2026-06-13)

- konflate chart **already at 0.2.16** (supports `statusChecks`/`prComments`) — no chart bump needed.
- konflate-external UI route (`konflate.${SECRET_DOMAIN}`) and webhook route (`/hooks` on envoy-external) already exist; `KONFLATE_WEBHOOK_SECRET` is wired → the GitHub-webhook render trigger is ready.
- konflate ExternalSecret currently provides `KONFLATE_TOKEN` (read-only PAT), `KONFLATE_PUSH_TOKEN`, `KONFLATE_WEBHOOK_SECRET`.
- konflate `ks.yaml` already includes the `gatus/guarded` component (health-check foundation for SPOF alerting).
- **No branch protection / rulesets exist** on the repo today (`gh api .../rulesets` → `[]`).
- Renovate rules are mixed: some `automergeType: "branch"` + `ignoreTests: true`, some `"pr"` + `ignoreTests: false`.

## What changes

### 1. konflate HelmRelease (`kubernetes/apps/flux-system/konflate/app/helmrelease.yaml`)
Add write-back config:
```yaml
config:
  repo: github://LukeEvansTech/talos-cluster
  statusChecks: true        # post the `Konflate` commit status
  prComments: true          # post/upsert the PR summary comment natively
  publicUrl: https://konflate.${SECRET_DOMAIN}
  # refreshInterval / logFormat unchanged
secret:
  existingSecret: konflate-secret   # now carries App creds (below)
```
The native `prComments` replaces the sticky comment that `konflate.yaml` posted.

### 2. konflate ExternalSecret (`.../konflate/app/externalsecret.yaml`)
Switch from the read PAT to GitHub App creds:
```yaml
data:
  KONFLATE_APP_CLIENT_ID: "{{ .KONFLATE_APP_CLIENT_ID }}"
  KONFLATE_APP_PRIVATE_KEY: "{{ .KONFLATE_APP_PRIVATE_KEY }}"
  KONFLATE_WEBHOOK_SECRET: "{{ .KONFLATE_WEBHOOK_SECRET }}"
```
`KONFLATE_TOKEN` / `KONFLATE_PUSH_TOKEN` are dropped (App handles read rate-limit + write-back; push-refresh not needed once webhook-driven). 1Password `konflate` item gains `KONFLATE_APP_CLIENT_ID` + `KONFLATE_APP_PRIVATE_KEY`.

### 3. GitHub App (manual, one-time — owner only)
Create App `konflate-bot`:
- Permissions: **Commit statuses: Read & write**, **Pull requests: Read & write**, **Contents: Read** (clone/fetch).
- Install on `LukeEvansTech/talos-cluster`.
- Put the **Client ID** and a generated **private key** into the 1Password `konflate` item (keys `KONFLATE_APP_CLIENT_ID`, `KONFLATE_APP_PRIVATE_KEY`).

### 4. Branch protection (manual or via API)
Add a ruleset / protection on `main` requiring status check **`Konflate`** (the default `statusCheckName`). Require only `Konflate`. Do **not** enable "require branches up to date" (would force-rebase every PR).

### 5. SPOF mitigation — konflate health alerting
Konflate is now load-bearing for merges. Ensure the `gatus/guarded` health check on konflate is alerting (route to the existing alertmanager). An outage should page fast, because it freezes merges. (See the 2026-06-13 phantom-mirror incident: a corrupt in-pod git mirror failed every render; fix was a pod restart.)

### 6. Renovate audit (`.renovaterc.json5` + `.renovate/`)
Reclassify auto-merge rules:
- **Manifest-affecting** (container images, Helm/OCI charts): `automergeType: "pr"` + `ignoreTests: false` → waits for `Konflate`.
- **Non-manifest** (GitHub Actions, mise tools, dashboards, presets): keep `automergeType: "branch"` + `ignoreTests: true`.
Branch protection is the hard backstop regardless of `ignoreTests`.

### 7. Delete CI workflows
- Delete `.github/workflows/flate.yaml`.
- Delete `.github/workflows/konflate.yaml`.
- Remove the `KONFLATE_URL` repo variable (only the deleted konflate.yaml used it).
- **Keep** `.github/workflows/image-pull.yaml` (uses `flate get images`).

## Migration sequence (avoids a window with no gate)

1. **Prep (PR):** konflate HelmRelease write-back config + ExternalSecret App-key wiring + Renovate audit. *Do not delete flate.yaml yet.* Merge while flate is still the gate.
2. **Owner step:** create `konflate-bot` App, install it, add creds to 1Password. Reconcile the konflate ExternalSecret + HelmRelease.
3. **Verify:** open a throwaway test PR; confirm konflate posts a `Konflate` commit status (green on a clean PR, red on a deliberately-broken render) and a native PR comment.
4. **Gate:** add the required `Konflate` branch-protection check once statuses are confirmed flowing.
5. **Cut over (PR):** delete `flate.yaml` + `konflate.yaml`; remove the `KONFLATE_URL` variable.
6. **Confirm:** a subsequent Renovate PR shows only `Konflate` (+ informational checks) and auto-merges after it goes green.

## Rollback

Every step is independently revertible:
- Remove the required `Konflate` check → merges instantly unblocked (covers a konflate outage).
- `flate.yaml` / `konflate.yaml` remain in git history → restore to bring back the CI gate.
- ExternalSecret revert restores `KONFLATE_TOKEN`; konflate config `statusChecks:false` reverts to advisory.

## Risks

- **konflate is now a SPOF for merges.** Mitigated by: health alerting (§5), the ability to drop the required check instantly (rollback), and `flate.yaml` recoverable from history.
- **Stale/missing status.** konflate posts asynchronously (webhook + refresh-interval backstop). A missed webhook delays the status until the next refresh; the required check stays pending, not failed — merge waits, doesn't break.
- **App credential loss.** App private key lives only in 1Password; losing it requires regenerating the key in the App settings.

## References

- onedr0p home-ops: `kubernetes/apps/flux-system/konflate/` (chart `oci://ghcr.io/home-operations/charts/konflate`, `statusChecks: true`, `publicUrl`, App auth via `github-bot`).
- konflate phantom-mirror incident (2026-06-13): pod restart clears a corrupt emptyDir git mirror.
- flate v0.4.x cascade: `.github/workflows/flate.yaml` pin comment; `docs/known-issues.md`.
