# KB-005: flate Mis-Resolves NGC HelmRepository Chart URLs (gpu-operator Skipped In CI)

**Status:** Worked around in CI; upstream fix pending. Remove the `gpu-operator` skip and restore plain `flate test all` once [home-operations/flate](https://github.com/home-operations/flate) fixes NGC `HelmRepository` relative-URL resolution.

## Symptom

CI's `Flate - Test` job (`.github/workflows/flate.yaml`, powered by [flate](https://github.com/home-operations/flate) instead of flux-local) loops `flate test all --namespace <ns>` over every directory under `kubernetes/apps/`. It **explicitly skips the `gpu-operator` namespace** with:

```text
::warning title=flate::skipping gpu-operator (flate NGC HelmRepository URL bug)
```

Without the skip, `flate test all --namespace gpu-operator` fails to pull the NVIDIA GPU Operator chart and the whole test job goes red.

## Cause

This is a **flate bug**, not a real chart/cluster problem. The GPU Operator is served from NVIDIA's NGC Helm registry via a `HelmRepository` (`helm.ngc.nvidia.com/nvidia`). NGC's index lists the chart with a **relative** URL:

```yaml
urls:
  - "charts/gpu-operator-v26.3.2.tgz"
```

Per the Helm spec a relative `urls:` entry resolves against the repository base URL — i.e. `helm.ngc.nvidia.com/nvidia/charts/gpu-operator-…tgz`. flate instead resolves it against the **host root**, requesting `helm.ngc.nvidia.com/charts/gpu-operator-…tgz` → **404**. The cluster's own Flux source-controller resolves the same `HelmRepository` correctly and pulls the chart fine, so this only affects the flate-based CI check.

## Fix

The per-namespace loop in CI skips `gpu-operator` (see the `TODO(flate)` marker in `.github/workflows/flate.yaml`). Everything else is still tested. To validate gpu-operator manifests locally, render the HelmRelease directly rather than via flate's chart pull, or rely on the live cluster's Flux reconcile.

Find the skip any time with:

```bash
grep -rn 'TODO(flate)' .github/
```
