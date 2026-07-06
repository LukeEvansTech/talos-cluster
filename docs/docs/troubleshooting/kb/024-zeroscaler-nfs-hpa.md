# KB-024: zeroscaler — NFS-availability scale-to-zero via native HPA

**Status:** Reference. Replaced the KEDA-based `nfs-scaler` (removed 2026-07). Pattern ported from
onedr0p/joryirving home-ops.

## Overview

NFS-dependent apps (the *arr stack, Plex, Jellyfin, qBittorrent, SABnzbd, …) are scaled to **0
replicas when the NFS server is unreachable** and back to **1** when it returns, so they don't
CrashLoopBackOff against a dead mount. This is done with a stock `autoscaling/v2`
HorizontalPodAutoscaler fed by `prometheus-adapter` — **no KEDA**.

## How it works

The chain is:

- **`blackbox-exporter` `lan-tcp-nfs` Probe** (`jobName: nfs_probe`) TCP-connects the NFS server on
  `:2049` every 1m, emitting `probe_success{job="nfs_probe"}`.
- **`prometheus-adapter`** (`observability` namespace) serves that metric on the
  `external.metrics.k8s.io` API, wrapped as `max_over_time(probe_success[3m])` — the **debounce**
  (see Gotchas).
- **`components/zeroscaler`** — a Kustomize component adding one HPA per app: `minReplicas: 0`,
  `maxReplicas: 1`, external metric `probe_success{job=nfs_probe}` target value `1`. Metric `1`
  → 1 replica; metric `0` → 0 replicas.
- An app opts in by adding `../../../../components/zeroscaler` to its `ks.yaml`
  `spec.components` (`${APP}` is already substituted). Override the probe with
  `ZEROSCALER_JOB_NAME` or the controller kind with `ZEROSCALER_CONTROLLER: StatefulSet`.

## Gotchas

- **`HPAScaleToZero` feature gate is required on BOTH the apiserver and the controller-manager.**
  Set `feature-gates: HPAScaleToZero=true` under **both** `cluster.apiServer.extraArgs` and
  `cluster.controllerManager.extraArgs` in `talos/patches/controller/cluster.yaml`. The apiserver
  **validates** `minReplicas` — without the gate there it **rejects** the HPA at apply
  (`spec.minReplicas: Invalid value: 0: must be greater than or equal to 1`), leaving the app's
  Flux Kustomization `Ready=False`; the controller-manager does the actual scale-to-0. flate/CI
  pass either way — it only surfaces when Flux applies the HPA. Changing either arg is a
  control-plane change (rolling restart); regenerate with `just talos gen-config` and
  `just talos apply-node` on each control-plane node.
- **`external.metrics.k8s.io` is a cluster singleton.** Only one APIService can back it. KEDA's
  metrics server and `prometheus-adapter` both claim it, so they **cannot coexist** — this is why
  the migration removed KEDA before adding the adapter.
- **The `[3m]` window is the flap fix.** A blackbox-exporter reschedule (e.g. during a Talos drain)
  can make a single scrape return `probe_success=0` from a DNS blip; `max_over_time(...[3m])` holds
  the metric at 1 through transient failures so the apps don't drop. See
  [KB-004](004-talos-patch-rollout-gotchas-tuppr.md).
- **`KubeHpaMaxedOut` is silenced cluster-wide.** Every HPA here is `maxReplicas: 1`, permanently
  "maxed" when NFS is up. The `zeroscaler-hpa-maxed` Silence matches `alertname=KubeHpaMaxedOut`
  (Alertmanager can't match the HPA's `app.kubernetes.io/part-of` label).
- **Drift detection stays off.** helm-controller `driftDetection` is intentionally disabled — a
  native HPA writes `spec.replicas` exactly as the KEDA HPA did, and enabling drift would fight it.

## Operations

Pin every NFS-gated app **up** for the duration of a node drain (native HPAs have no `paused`
annotation — the recipe patches `minReplicas: 1`):

```bash
just kube zeroscaler suspend   # pin all zeroscaler HPAs up (minReplicas=1)
just kube zeroscaler resume    # back to metric-driven (minReplicas=0)
```

Flux reverts `minReplicas` to `0` on the next reconcile, so pause then act promptly, or
`flux suspend kustomization <app> -n <ns>` for a longer hold.

Inspect state:

```bash
kubectl get hpa -A -l app.kubernetes.io/part-of=zeroscaler
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/observability/probe_success?labelSelector=job=nfs_probe"
```
