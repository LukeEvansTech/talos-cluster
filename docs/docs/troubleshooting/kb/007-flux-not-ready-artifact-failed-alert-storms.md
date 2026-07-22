# KB-007: Flux "not ready" / "artifact failed" Alert Storms

**Status:** Both patterns understood; either can recur under load or an upstream blip.

A sudden flood of Flux alerts almost always traces back to **one** root cause cascading,
not dozens of independent failures. The workloads themselves usually stay healthy: the
break is at the Flux source / health-gate layer. Two distinct triggers produce the storm;
the alert text tells them apart.

## Symptom

- **Variant A: `FluxHelmReleaseArtifactFailed` cluster-wide** (informally "flux is OOM").
  Many HelmReleases report `Could not load chart: GET http://source-controller.../ocirepository/... context deadline exceeded`.
- **Variant B: ~28 "dependency not ready" / Kustomization `Ready=False` alerts** at once,
  each re-firing on every retry.

In both, `kubectl get pods -A` is green, nodes are `Ready`, and the apps keep serving.

## Cause

### Variant A: source-controller OOMKilled after a merge burst

A burst of merges (e.g. ~14 PRs in quick succession) spikes source-controller: artifact-store
rebuild, `--helm-cache-max-size`, and `--concurrent` reconciles together push it past its
memory limit. Once it OOM-crashloops it **cannot serve OCI/git artifacts**, so every
HelmRelease that needs to (re)load its chart fails: all the ArtifactFailed alerts are
downstream of the single OOM.

### Variant B: a transient registry outage cascading via `dependsOn`

A brief upstream registry blip (e.g. a `quay.io` **504 Gateway Timeout**) breaks the digest
re-fetch for a source that sits at the **root** of a `dependsOn` chain (cert-manager and
Cilium are quay-hosted; everything on ghcr.io stays fine). A root Kustomization with
`wait: true` + `healthCheckExprs` flips `Ready=Unknown/False`, and because cert-manager is a
dependency root for much of the tree, one failing source cascades into ~28 dependents.

The discriminating tell: the **HelmRelease** stays `Ready` (it runs off the cached chart);
only the **OCIRepository / HelmChart** source is unready.

## Fix

### Variant A: raise source-controller memory (and break-glass the deadlock)

Bump the source-controller resources in the FluxInstance patch at
`kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml` (under
`spec.values.instance.kustomize.patches`, `target.name: source-controller`). Steady state is
~400Mi and a full artifact re-fetch peaks ~474Mi, so 512Mi is too tight. Use **1Gi**
(request = limit). Go higher if a bigger burst OOMs it again.

The git fix can't self-apply while source-controller is down (kustomize/helm-controller need
it to fetch the git artifact and re-render). Break the deadlock by editing the **live**
FluxInstance CR. **flux-operator**, not source-controller, reconciles it, so it applies
independently:

```bash
kubectl get fluxinstance flux -n flux-system -o json > /tmp/fluxinstance.json
# edit the source-controller memory patch 256Mi -> 1Gi in /tmp/fluxinstance.json
kubectl replace -f /tmp/fluxinstance.json
```

Then land the same value in git and `flux reconcile kustomization flux-instance -n flux-system`
so live and git converge (otherwise the next re-render reverts the break-glass).

### Variant B: retry the failing root source

The sources self-heal once the registry recovers; just nudge them and let the cascade clear:

```bash
flux reconcile source oci cert-manager -n cert-manager
flux reconcile source oci cilium -n kube-system
flux reconcile kustomization cert-manager -n cert-manager
```

Diagnose which variant you have first: `flux get kustomizations -A --status-selector ready=false`,
trace the `dependsOn` chain to its root, then check that root's **OCIRepository/HelmChart**
readiness (not its HelmRelease). A secondary aggravator on Variant B: every `cluster-secrets`
ExternalSecret refresh cancels in-flight health checks and restarts the 15m clock, prolonging
recovery: inherent to `dependsOn`, no clean config fix.

## References

- Flux source-controller flags: <https://fluxcd.io/flux/components/source/>
- Related OOM-by-repo-size pattern: [KB-016](016-kopia-repo-server-oom-repo-size.md)
