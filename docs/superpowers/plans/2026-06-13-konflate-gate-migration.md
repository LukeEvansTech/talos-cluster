# Konflate Gate Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make konflate's native `Konflate` commit status the single required render gate, and retire the `flate test`/`diff` CI gate.

**Architecture:** In-cluster konflate (chart 0.2.16, already deployed) authenticates via a GitHub App and posts a `Konflate` commit status + PR comment per render. A branch-protection rule makes `Konflate` required. `flate.yaml` + `konflate.yaml` workflows are deleted; `flate` survives only in `image-pull.yaml`. Renovate auto-merge rules convert to PR-type so they respect branch protection.

**Tech Stack:** Flux (flux-operator), konflate Helm chart, External Secrets + 1Password, GitHub App, GitHub branch-protection rulesets, Renovate.

**Spec:** `docs/flate-to-konflate-migration.md` (rationale, decisions, rollback).

**Verification note:** This is a GitOps change, so "tests" are local renders (`flate build` / `kustomize build`) and live-cluster/PR checks, not unit tests. Each manifest task validates by rendering before commit and by observing cluster/PR state after merge.

**Ordering constraint:** Task 1 (GitHub App + 1Password creds) MUST complete before Task 2 merges — the Task 2 ExternalSecret references `KONFLATE_APP_*` keys that ESO will fail to sync if they don't exist in 1Password yet.

---

## File Structure

- `kubernetes/apps/flux-system/konflate/app/externalsecret.yaml` — swap read PAT for App creds (Task 2)
- `kubernetes/apps/flux-system/konflate/app/helmrelease.yaml` — enable `statusChecks`/`prComments`/`publicUrl` (Task 2)
- `.github/workflows/flate.yaml` — delete (Task 5)
- `.github/workflows/konflate.yaml` — delete (Task 5)
- `.renovaterc.json5` — convert `automergeType: branch` → `pr` (Task 5)
- GitHub App + branch-protection ruleset + `KONFLATE_URL` variable — GitHub-side, not in git (Tasks 1, 4, 5)

---

## Task 1: Create the konflate-bot GitHub App (MANUAL — owner only)

**No files. GitHub-side + 1Password.** Claude cannot do this; the owner performs it.

- [ ] **Step 1: Create the App**

Go to https://github.com/settings/apps/new (or org settings). Set:
- Name: `konflate-bot` (or similar; the bot identity for statuses/comments)
- Homepage URL: `https://konflate.<your-domain>`
- Webhook: **uncheck Active** (konflate's own webhook is separate)
- Repository permissions:
  - **Commit statuses: Read and write**
  - **Pull requests: Read and write**
  - **Contents: Read-only**
- Where can this app be installed: Only this account.

- [ ] **Step 2: Generate a private key**

On the App page → "Private keys" → "Generate a private key". A `.pem` downloads.

- [ ] **Step 3: Install the App on the repo**

App page → "Install App" → select `LukeEvansTech/talos-cluster`.

- [ ] **Step 4: Store creds in 1Password (Talos vault, `konflate` item)**

Add two fields to the existing `konflate` item:
```bash
# Client ID is on the App's General page (e.g. Iv23xxxxxxxx)
op item edit konflate --vault Talos KONFLATE_APP_CLIENT_ID=<client-id>
# Private key: paste the full PEM contents (BEGIN/END lines included)
op item edit konflate --vault Talos KONFLATE_APP_PRIVATE_KEY="$(cat ~/Downloads/konflate-bot.*.private-key.pem)"
```

- [ ] **Step 5: Verify the 1Password keys exist**

Run:
```bash
op item get konflate --vault Talos --fields KONFLATE_APP_CLIENT_ID
op item get konflate --vault Talos --fields KONFLATE_APP_PRIVATE_KEY --format json | head -c 80
```
Expected: client ID prints; private key field exists (PEM begins `-----BEGIN`).

---

## Task 2: Enable konflate write-back (PR — prep, while flate still gates)

**Files:**
- Modify: `kubernetes/apps/flux-system/konflate/app/externalsecret.yaml`
- Modify: `kubernetes/apps/flux-system/konflate/app/helmrelease.yaml`

- [ ] **Step 1: Branch**

```bash
cd /Users/luke.evans/GIT/LukeEvansTech/talos-cluster
git checkout main && git pull --ff-only
git checkout -b feat/konflate-writeback
```

- [ ] **Step 2: Rewrite the ExternalSecret data block**

In `externalsecret.yaml`, replace the `target.template.data` block so it carries the App creds (loaded into konflate via `envFrom: secretRef: konflate-secret`) and drops the read PAT + push token:

```yaml
  target:
    name: konflate-secret
    template:
      engineVersion: v2
      data:
        # GitHub App identity — konflate uses these for read (rate-limit lift)
        # AND write-back (commit statuses + PR comments). Loaded via envFrom.
        KONFLATE_APP_CLIENT_ID: "{{ .KONFLATE_APP_CLIENT_ID }}"
        KONFLATE_APP_PRIVATE_KEY: "{{ .KONFLATE_APP_PRIVATE_KEY }}"
        # HMAC secret validating GitHub webhook deliveries on POST /hooks
        KONFLATE_WEBHOOK_SECRET: "{{ .KONFLATE_WEBHOOK_SECRET }}"
  dataFrom:
    - extract:
        key: konflate
```

- [ ] **Step 3: Enable write-back in the HelmRelease**

In `helmrelease.yaml`, replace the `config:` block under `values:` with (keep the existing comment intent + add the three write-back keys):

```yaml
    config:
      # Public repo → App auth lifts the API rate limit and supplies the
      # write identity for status checks + PR comments.
      repo: github://LukeEvansTech/talos-cluster
      refreshInterval: 15m
      logFormat: json
      statusChecks: true                              # post the `Konflate` commit status
      prComments: true                                # post the PR summary comment natively
      publicUrl: "https://konflate.${SECRET_DOMAIN}"  # status/comment deep-links
```

(`statusCheckName` is left unset → defaults to `Konflate`, the name we'll require in Task 4.)

- [ ] **Step 4: Render locally to validate the manifests**

Run:
```bash
flate build hr konflate -n flux-system --path kubernetes/flux/cluster --allow-missing-secrets 2>&1 | grep -E "statusChecks|prComments|publicUrl|envFrom|konflate-secret" | head
```
Expected: HR renders without error; the Deployment shows `envFrom` → `konflate-secret`. (Use the repo-pinned flate, currently v0.3.3 — render-only, no gate.)

- [ ] **Step 5: Commit + push + PR**

```bash
git add kubernetes/apps/flux-system/konflate/app/externalsecret.yaml \
        kubernetes/apps/flux-system/konflate/app/helmrelease.yaml
git commit -m "feat(konflate): enable native status-check + PR-comment write-back (GitHub App)"
git push -u origin feat/konflate-writeback
gh pr create --fill
```

- [ ] **Step 6: Wait for CI green, merge** (flate is still the gate here)

```bash
gh pr checks <pr> --watch
gh pr merge <pr> --squash --delete-branch
```

- [ ] **Step 7: Reconcile + verify ESO sync and konflate env**

```bash
export KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig
flux reconcile source git flux-system
flux reconcile kustomization konflate -n flux-system
kubectl get externalsecret konflate -n flux-system   # expect SecretSynced=True
kubectl get secret konflate-secret -n flux-system -o go-template='{{range $k,$_ := .data}}{{$k}}{{"\n"}}{{end}}'
```
Expected: `KONFLATE_APP_CLIENT_ID`, `KONFLATE_APP_PRIVATE_KEY`, `KONFLATE_WEBHOOK_SECRET` present; no `KONFLATE_TOKEN`. konflate pod restarts cleanly (`kubectl get pods -n flux-system -l app.kubernetes.io/name=konflate`).

---

## Task 3: Verify konflate posts the `Konflate` status (verification gate)

**No files. Live PR test.**

- [ ] **Step 1: Open a throwaway PR with a real (clean) manifest change**

```bash
git checkout main && git pull --ff-only
git checkout -b test/konflate-status
# trivial no-op-ish change that still renders, e.g. a comment line in any app yaml
printf '\n# konflate status smoke test\n' >> kubernetes/apps/default/echo-server/app/helmrelease.yaml
git commit -am "test: konflate status smoke test" && git push -u origin test/konflate-status
gh pr create --fill
```

- [ ] **Step 2: Confirm the `Konflate` commit status appears and is green**

```bash
sleep 30
gh pr checks <pr> | grep -i konflate
```
Expected: a `Konflate` status (posted by konflate-bot, not the konflate.yaml job) reporting success. Also confirm a native konflate PR comment from `konflate-bot[bot]`.

- [ ] **Step 3: (Optional) Confirm a broken render reports failure**

Push a deliberately invalid change (e.g. a bad indent in a values block) and confirm `Konflate` flips to failure. Then revert.

- [ ] **Step 4: Close the test PR + delete branch**

```bash
gh pr close <pr> --delete-branch
```

---

## Task 4: Require the `Konflate` check (MANUAL/API — gate goes live)

**No files. GitHub branch-protection ruleset.** Do this only after Task 3 confirms statuses flow.

- [ ] **Step 1: Create a ruleset on `main` requiring `Konflate`**

```bash
gh api -X POST repos/LukeEvansTech/talos-cluster/rulesets \
  -f name='require-konflate' -f target='branch' -f enforcement='active' \
  -F 'conditions[ref_name][include][]=~DEFAULT_BRANCH' \
  -F 'rules[][type]=required_status_checks' \
  -F 'rules[][parameters][strict_required_status_checks_policy]=false' \
  -F 'rules[][parameters][required_status_checks][][context]=Konflate'
```
(Do NOT enable `strict` / "require up to date" — it would force-rebase every PR.)

- [ ] **Step 2: Verify the ruleset is active**

```bash
gh api repos/LukeEvansTech/talos-cluster/rulesets | python3 -c "import json,sys;[print(r['name'],r['enforcement']) for r in json.load(sys.stdin)]"
```
Expected: `require-konflate active`.

- [ ] **Step 3: Confirm konflate health alerting (SPOF mitigation)**

`Konflate` is now load-bearing for merges, so a konflate outage must page fast. The konflate `ks.yaml` already includes the `gatus/guarded` component. Verify the health check exists and is monitored:
```bash
export KUBECONFIG=/Users/luke.evans/GIT/LukeEvansTech/talos-cluster/kubeconfig
kubectl get configmap -n observability -l gatus.io/enabled --no-headers 2>/dev/null | grep -i konflate || \
  kubectl get gatus -A 2>/dev/null | grep -i konflate
# Confirm the gatus endpoint for konflate is UP and an alert route exists
```
Expected: a gatus endpoint for konflate exists and is healthy. If konflate has no alerting endpoint, add one to its `ks.yaml` `gatus/guarded` config so a down konflate fires an alertmanager alert (see `kubernetes/components/gatus/guarded`). This is the agreed mitigation for the required-check SPOF.

---

## Task 5: Cut over — delete flate/konflate workflows + Renovate audit (PR)

**Files:**
- Delete: `.github/workflows/flate.yaml`
- Delete: `.github/workflows/konflate.yaml`
- Modify: `.renovaterc.json5`
- GitHub-side: remove `KONFLATE_URL` variable

This PR is itself gated by the new `Konflate` required check (dogfood).

- [ ] **Step 1: Branch + delete the two workflows**

```bash
git checkout main && git pull --ff-only
git checkout -b feat/retire-flate-gate
git rm .github/workflows/flate.yaml .github/workflows/konflate.yaml
```

- [ ] **Step 2: Renovate audit — convert all `automergeType: "branch"` to `"pr"`**

A required `Konflate` check blocks direct-to-branch pushes, so branch-automerge would stall. In `.renovaterc.json5`, change every `automergeType: "branch"` to `automergeType: "pr"`. The affected rules (and their intended `ignoreTests`):

| Rule (`description`) | `automergeType` | `ignoreTests` |
|---|---|---|
| Auto-merge GitHub Actions | `pr` | `true` (non-manifest; Konflate still gates via GitHub) |
| Auto-merge GitHub Releases (external-dns/gateway-api/prometheus-operator) | `pr` | **`false`** (these affect rendered CRDs/manifests) |
| Auto-merge Mise Tools | `pr` | `true` (tooling, non-manifest) |
| Auto-merge trusted GitHub Actions (`actions/*`,`renovatebot/*`) | `pr` | `true` |
| Grafana dashboards (major) | `pr` | `true` |

All rules already on `automergeType: "pr"` (OCI Charts, trusted container digests, changedetection/whodb/databasus/seerr) keep `ignoreTests: false` — unchanged.

Apply with targeted edits (one per occurrence; verify each diff):
```bash
# review each before/after — do NOT blind sed, the github-releases rule also flips ignoreTests
grep -n 'automergeType: "branch"' .renovaterc.json5
```
Edit each `automergeType: "branch"` → `"pr"`, and on the **GitHub Releases** rule (matchDatasources github-releases, lines ~92-103) also change `ignoreTests: true` → `ignoreTests: false`.

- [ ] **Step 3: Validate the renovate config parses**

Run:
```bash
npx --yes renovate-config-validator .renovaterc.json5
```
Expected: "Config validated successfully".

- [ ] **Step 4: Commit + push + PR**

```bash
git add .github/workflows .renovaterc.json5
git commit -m "feat(ci): retire flate gate; konflate is the required render check

Deletes flate.yaml (test/diff gate) + konflate.yaml (advisory CI job) now
that konflate posts the required Konflate status natively. Converts Renovate
auto-merge rules to PR-type so they honour the new branch protection."
git push -u origin feat/retire-flate-gate
gh pr create --fill
```

- [ ] **Step 5: Confirm THIS PR is gated by `Konflate` and merge**

```bash
gh pr checks <pr> | grep -iE "konflate|flate"
```
Expected: a `Konflate` check present (required); NO `Flate - *` checks (workflow deleted on this branch). Merge once `Konflate` is green:
```bash
gh pr merge <pr> --squash --delete-branch
```

- [ ] **Step 6: Remove the now-unused repo variable**

```bash
gh variable delete KONFLATE_URL --repo LukeEvansTech/talos-cluster
```

---

## Task 6: Confirm steady-state

- [ ] **Step 1: A real Renovate PR gates on Konflate and auto-merges**

Wait for the next Renovate PR (or trigger Renovate). Verify:
```bash
gh pr checks <renovate-pr> | grep -iE "konflate|flate"
```
Expected: `Konflate` present + required; no `Flate - *`. A manifest-affecting PR auto-merges only after `Konflate` is green; a github-actions/mise PR auto-merges via PR after Konflate (trivially green) passes.

- [ ] **Step 2: Confirm flate binary only remains in image-pull**

```bash
grep -rln "flate" .github/workflows/
```
Expected: only `image-pull.yaml`.

- [ ] **Step 3: Update the spec status**

Mark `docs/flate-to-konflate-migration.md` as completed (add a one-line "Completed: <date>" under Overview) and commit.

---

## Rollback (any time)

- Konflate misbehaving / outage blocking merges → delete the ruleset: `gh api -X DELETE repos/LukeEvansTech/talos-cluster/rulesets/<id>`. Merges instantly unblocked.
- Need the CI gate back → `git revert` the Task 5 commit (restores `flate.yaml` + `konflate.yaml`); restore `KONFLATE_URL` variable.
- konflate write-back wrong → set `statusChecks: false` in the HelmRelease (reverts to advisory) and revert the ExternalSecret to `KONFLATE_TOKEN`.
