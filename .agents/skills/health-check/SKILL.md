---
name: health-check
description: Read-only post-change health verdict for the talos-cluster (healthy / benign-warn / regression / blind), diffed against the previous snapshot
---

# Cluster health verdict

Run the cross-cutting health checks from `docs/docs/operations/health-verdict.md` against the live
cluster, compare with the previous snapshot, and report a verdict. **Read-only**: nothing in this
skill mutates the cluster, and it must not be extended to.

## When to use

- After a protected-infra merge (anything the "Protected infra" rule in `.renovaterc.json5`
  matches), a Talos or Kubernetes roll, or a batch of Renovate merges.
- When an alert storm needs a second opinion on whether anything is actually broken.
- Before starting a Talos roll (tuppr gates on Ceph and VolSync; this shows the blocker early).

## Steps

1. Take a snapshot:

   ```bash
   SNAP_DIR="${SCRATCHPAD:-/tmp}/health"
   mkdir -p "$SNAP_DIR"
   bash .agents/skills/health-check/snapshot.sh > "$SNAP_DIR/$(date -u +%Y%m%dT%H%M%SZ).json"
   ```

   The script needs the repository kubeconfig (exported by `.mise.toml`, or set `KUBECONFIG` to
   `./kubeconfig`), `kubectl`, `flux` and `jq`. Every check that fails to run is recorded as
   `"blind": true` for that key instead of aborting the run.

2. If a previous snapshot exists in `$SNAP_DIR` (or one is named in the request), diff the two
   without writing anything outside `$SNAP_DIR`: `diff <(jq -S . "$OLD") <(jq -S . "$NEW")`. A raw
   snapshot names pods and namespaces, so it must never land in the checkout where a later
   `git add` could publish it. Interesting deltas are new entries in any `not_ready` /
   `not_running` / `stale` / `critical` / `failing` list, and any pod whose restart count rose.

   **No previous snapshot means no verdict.** Restart deltas, "new since the change" and
   "pre-existing" are all defined against a baseline. On a first run record the snapshot, report
   `baseline` with the absolute findings (anything blind, anything failing outright), and say a
   healthy verdict needs a second snapshot to compare against.

3. Classify every non-green item using the tables on the health-verdict page and the
   [known noise](../../../docs/docs/troubleshooting/known-noise.md) page. Re-poll transients
   once after about 90 seconds (image pulls, a reconcile still in progress) before deciding.

4. Report, in this order:
   - **Verdict:** one of `healthy`, `benign-warn`, `regression`, `blind`, `baseline`. A single
     blind check makes the whole verdict `blind` unless the user explicitly accepts the gap; a
     first run with nothing to diff against is `baseline`, never `healthy`.
   - **New since last snapshot:** each item with the check it came from and its classification.
     For a benign-warn, name the known-noise entry that matched.
   - **Pre-existing:** items present in both snapshots, one line each, no analysis.
   - **What to do next:** for a regression, the playbook section
     (`docs/docs/operations/upgrade-playbooks.md`) for the component that changed; for blind, the
     command that failed and its error text.

5. Do **not** remediate as part of this skill, even for items on the "Not noise: remediated with a
   known fix" table. Name the remedy and let the user (or the session that owns the change) apply
   it.

## Interpretation rules

- A check that printed nothing because it errored is **blind**, never green. The script encodes
  this; when running commands by hand, check the exit code before reading an empty result as
  quiet.
- Diff against the prior snapshot before diffing against "ideal". A critical alert that was
  firing before the change is context, not a regression.
- `startsAt` on an Alertmanager alert resets when Alertmanager restarts. Several alerts sharing
  one `startsAt` date that restart, not the fault.
- Ceph `HEALTH_OK (muted: AUTH_INSECURE_...)` is healthy. The mutes are deliberate.
- The `CephCluster` CR's health lags the toolbox by minutes after a reboot; the script uses the
  toolbox.
