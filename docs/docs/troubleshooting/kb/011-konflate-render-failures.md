# KB-011: konflate Render Failures (Cache Inode Fill / Phantom Mirror)

**Status:** Both resolved; the operational lessons (never grow a GitOps PVC imperatively;
restart to wipe the emptyDir mirror) are durable.

`konflate` (chart `oci://ghcr.io/home-operations/charts/konflate`) runs as a Flux HelmRelease
in `flux-system`, rendering in-cluster Flux diffs over the repository. It caches many tiny
OCI/Helm files and clones the repo into an in-pod git mirror. Two different failure modes have
hit it.

## Symptom

- **Variant A — `KubePersistentVolumeInodesFillingUp` (critical)** on `konflate-cache` within
  ~a day, with plenty of free **bytes** remaining.
- **Variant B — every open PR's konflate check fails** with `status: error` ("all my CIs are
  failing"). Pod logs show the **same** object SHA across all PRs:
  `gitclone: repack mirror: getting object <SHA> failed: object not found`.

## Cause

### Variant A — inode density, not size

konflate's source/render/stage caches are millions of tiny files at only a few GB, so a
fixed-inode ceph-block RBD volume **exhausts inodes long before bytes**. The chart bounds
caches by bytes + TTL but never by inode count.

### Variant B — a phantom (dangling) ref in the git mirror

The failing SHA is a **phantom** — not in the GitHub repo at all (`git cat-file -t <SHA>` =
bad object even after fetch). It's a dangling ref in konflate's in-pod mirror, almost
certainly a commit that Renovate force-pushed away but the mirror still references.
`repack mirror` chokes on it, which breaks the **whole** mirror, so every render fails
globally.

## Fix

### Variant A — use an emptyDir cache, never `kubectl patch` the PVC

Set `persistence.enabled: false` (the chart default) so the cache mounts an **emptyDir** on the
node filesystem (Talos `/var` is xfs with dynamic inodes); helm then deletes the
`konflate-cache` PVC and the alert clears at source. Trade-off: open-PR diffs re-render and the
merged-PR shelf is lost on restart.

> **Hard-won rule: never grow a GitOps-managed PVC with `kubectl patch`.** An imperative
> `5Gi → 20Gi` grow clears the alert but **diverges the live volume from git's declared size**.
> The next chart bump re-applies the PVC at the git size and the helm upgrade fails —
> `spec.resources.requests.storage: Forbidden: field can not be less than status.capacity`
> (PVCs can't shrink) — which then fails the auto-rollback and leaves the HR stuck
> `Ready=False/RollbackFailed`. Always bump `size:` **in git**. ("Out of inodes" with free
> bytes is an inode-density mismatch, not a sizing problem — prefer emptyDir on xfs for
> many-tiny-file caches.)

### Variant B — restart the pod to wipe the mirror

Persistence is disabled (emptyDir mirror), so a restart wipes the corrupt mirror and
re-clones cleanly:

```bash
kubectl rollout restart deployment/konflate -n flux-system
```

Renders resume in under a minute. A server-side fix does **not** re-run the failed GitHub
checks: `gh run rerun <id> --failed` only works if that PR's run used the *current* workflow
file; for older runs use `gh pr close <n> && gh pr reopen <n>` to get a fresh run against
current `main`.

## konflate status semantics

konflate is a **lenient** gate, kept advisory (not required) — `flate` remains the strict
gate. Its render-status header maps to:

- `ok` → pass.
- `failures` → individual resources couldn't render (e.g. a private source). Made advisory
  (`::warning`, no failure) — expected on most PRs, not a bug.
- `error` → the whole render failed with no diff → the real gate. The phantom-object failure
  is this one.

konflate does **not** schema-validate (it passes a nonexistent chart tag and a non-int
`replicas`), which is why it can't replace `flate`.

## References

- konflate: <https://github.com/home-operations/konflate>
- GitHub API rate limits (anonymous = 60 req/hr/IP; authenticate konflate to get 5000).
