# KB-015: Slow Image Pulls Exceed the HelmRelease Timeout (Rollback Loop)

**Status:** Recurring pattern; the `spec.timeout: 15m` fields it leaves behind are
load-bearing. Don't strip them.

## Symptom

A HelmRelease shows `UpgradeFailed: timeout waiting for Deployment ... status: 'InProgress'`
followed by a rollback (and sometimes `RollbackFailed` / `Stalled`), typically right after a
Renovate image bump. The pod is stuck `ContainerCreating` with an active `Pulling` event.

## Cause

It's not the app. It's the pull. This cluster's link to GHCR sustains roughly **~1 MB/s**, so
any image in the **250 MB+** range takes longer than Helm's **default 5-minute** upgrade
timeout to pull. Each failed attempt **restarts the pull from zero** (the partial pull is
discarded on rollback), so layers never cache and retries don't converge.

## Fix

Raise the HelmRelease timeout:

```yaml
spec:
  interval: 1h
  timeout: 15m       # goes right after interval:, before chartRef:
  chartRef:
    ...
```

After merge, reconcile the Kustomization; a `Stalled` HR exits and retries the upgrade
automatically.

HRs already carrying this for large images include `default/giteamirror` (~244 MB) and
`default/n8n` (~310 MB).

## How to apply

1. On any `UpgradeFailed`/`RollbackFailed`/`Stalled` after a bump, first check for a pod stuck
   `ContainerCreating` with a live `Pulling` event: if so it's this pattern, **not** an app
   bug.
2. Add `spec.timeout: 15m` to the HelmRelease.
3. **Don't strip these `timeout` fields** when refactoring: they are load-bearing.
4. When onboarding a new app whose image is known to be large (> 250 MB), set `timeout: 15m`
   preemptively.

## References

- Flux HelmRelease `spec.timeout`:
  <https://fluxcd.io/flux/components/helm/helmreleases/#configuring-failure-handling>
