# KB-005: flate Mis-Resolves NGC HelmRepository Chart URLs (gpu-operator Skipped In CI)

> **Status: Resolved.** gpu-operator was migrated to an OCI community mirror,
> eliminating the flate bug and the CI workaround. Preserved as a resolved-incident record.

**Previous status:** Worked around in CI; upstream fix pending.

## Symptom

CI's `Flate - Test` job (`.github/workflows/flate.yaml`, powered by
[flate](https://github.com/home-operations/flate) instead of flux-local)
**previously** looped `flate test all --namespace <ns>` over every directory
under `kubernetes/apps/` and explicitly skipped the `gpu-operator` namespace to
avoid a hard failure pulling the NGC chart.

**Current behaviour:** The job runs a single command with no per-namespace loop
and no skip:

```bash
flate test all --path kubernetes/flux/cluster --allow-missing-secrets
```

A once-retry is included for occasional flakiness (substitution-cache
nondeterminism), but there is no `gpu-operator` exclusion and no `TODO(flate)`
marker anywhere in `.github/`.

## Cause

This was a **flate bug**, not a real chart/cluster problem. The GPU Operator was
served from NVIDIA's NGC Helm registry via a `HelmRepository`
(`helm.ngc.nvidia.com/nvidia`). NGC's index listed the chart with a **relative**
URL:

```yaml
urls:
  - "charts/gpu-operator-v26.3.2.tgz"
```

Per the Helm spec a relative `urls:` entry resolves against the repository base
URL, i.e. `helm.ngc.nvidia.com/nvidia/charts/gpu-operator-…tgz`. flate instead
resolved it against the **host root**, requesting
`helm.ngc.nvidia.com/charts/gpu-operator-…tgz` → **404**. In-cluster Flux
source-controller resolved the same `HelmRepository` correctly (so deployments
were unaffected), but flate-based CI always failed on the chart pull.

## Fix (Resolution)

gpu-operator was migrated from the NGC `HelmRepository` to an OCI community
mirror (`ghcr.io/home-operations/charts-mirror/nvidia-gpu-operator`). The
`HelmRelease` now references an `OCIRepository` source, bypassing the NGC
`HelmRepository` entirely:

```yaml
# kubernetes/apps/gpu-operator/gpu-operator/app/ocirepository.yaml
spec:
  url: oci://ghcr.io/home-operations/charts-mirror/nvidia-gpu-operator
```

```yaml
# kubernetes/apps/gpu-operator/gpu-operator/app/helmrelease.yaml
spec:
  chartRef:
    kind: OCIRepository
    name: gpu-operator
```

With the NGC `HelmRepository` no longer in the resolution path, flate's
relative-URL bug stopped affecting CI. The per-namespace skip was removed and
CI was simplified to a single unconditional `flate test all --path
kubernetes/flux/cluster --allow-missing-secrets` run.

The community mirror is intended as a temporary measure; NVIDIA's native OCI
artifact support is tracked upstream in `NVIDIA/gpu-operator#2520`. The mirror
deprecates six months after official OCI artifacts land.
