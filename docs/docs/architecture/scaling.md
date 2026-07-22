# Scaling

The cluster runs one deliberately narrow autoscaling pattern, **zeroscaler**: an app scales to
**0 replicas when its NFS backing store is unreachable** and back to **1** when it returns. It is a
dependency-availability gate, not load-based autoscaling. There are no CPU/memory HPAs, and no
KEDA (removed 2026-07 in favour of a native HPA).

The point is to stop NFS-dependent apps from CrashLoopBackOff-ing against a dead mount (e.g. while
the NAS reboots), and to let them self-heal when it comes back.

## How it works

```text
blackbox-exporter  lan-tcp-nfs Probe (jobName: nfs_probe)   →  probe_success{job="nfs_probe"}
        │                                                        (Prometheus)
        ▼
prometheus-adapter (observability)  →  serves external.metrics.k8s.io
        rule: max_over_time(probe_success[3m])                  ← debounce
        │
        ▼
HorizontalPodAutoscaler per app (components/zeroscaler)
        minReplicas 0, maxReplicas 1, external metric target 1
        │
        ▼
Deployment/<app>   →  metric 1 = 1 replica,  metric 0 = 0 replicas
```

- The blackbox `tcp_connect` probe hits the NFS server on `:2049` every 1m. `jobName: nfs_probe`
  gives the series a stable `job="nfs_probe"` label for the HPA selector.
- `prometheus-adapter` translates that Prometheus series into a Kubernetes external metric. The
  `max_over_time(...[3m])` wrapper is a **debounce**: a single transient `probe_success=0` (e.g. a
  blackbox pod reschedule during a Talos drain) no longer trips scale-to-0; the probe must fail
  continuously for ~3 minutes. See [KB-004](../troubleshooting/kb/004-talos-patch-rollout-gotchas-tuppr.md).
- The HPA (`autoscaling/v2`, min 0 / max 1) tracks the metric: `1` holds one replica, `0` scales to
  zero.

## Gated apps

Nine NFS-dependent apps are gated: **media**: plex, jellyfin, sonarr, radarr, bazarr, pinchflat,
wizarr; **downloads**: qbittorrent, sabnzbd.

## Adding an app to the gate

Add the component to the app's `ks.yaml` `spec.components` (the `${APP}` substitution is already
set on every app Kustomization):

```yaml
spec:
  components:
    - ../../../../components/zeroscaler
```

Optional per-app overrides via `postBuild.substitute`:

- `ZEROSCALER_JOB_NAME`: gate on a different blackbox probe (e.g. a Zigbee coordinator instead of
  NFS).
- `ZEROSCALER_CONTROLLER: StatefulSet`: if the app is a StatefulSet rather than a Deployment.

## Prerequisites

The `HPAScaleToZero` feature gate must be enabled on **both** `cluster.apiServer.extraArgs` **and**
`cluster.controllerManager.extraArgs` in `talos/patches/controller/cluster.yaml`. The apiserver
validates `minReplicas` (and rejects `0` without the gate); the controller-manager does the actual
scale-to-0. Missing it on the apiserver is the classic failure: see
[KB-024](../troubleshooting/kb/024-zeroscaler-nfs-hpa.md).

## Operations

- **Pause during a node drain**: native HPAs have no `paused` annotation, so pin every gated app
  up with the recipe (it patches `minReplicas: 1`):

  ```bash
  just kube zeroscaler suspend   # pin all NFS-gated apps up
  just kube zeroscaler resume    # back to metric-driven
  ```

  Flux reverts `minReplicas` to `0` on the next reconcile, so pause then act promptly (or
  `flux suspend` the app's Kustomization for a longer hold).
- **`KubeHpaMaxedOut` is silenced cluster-wide**: every HPA here is `maxReplicas: 1`, permanently
  "maxed" when NFS is up (silence `zeroscaler-hpa-maxed`).
- **helm-controller drift detection stays off**: a native HPA writes `spec.replicas`, and drift
  detection would fight it.

## Why this shape

- **No load-based autoscaling.** Home-cluster workloads are singletons; scaling on CPU/memory
  isn't needed. The only useful signal is "is the backing store there?".
- **No KEDA.** It was a whole operator (+ CRDs + metrics server + webhooks) for this one binary
  pattern. A native HPA plus `prometheus-adapter` is lighter and keeps the singleton
  `external.metrics.k8s.io` APIService free. KEDA's metrics server and the adapter cannot co-own
  it.

Deep dive and gotchas: [KB-024](../troubleshooting/kb/024-zeroscaler-nfs-hpa.md).
Related: [Storage](storage.md), [KB-004 (flap during drains)](../troubleshooting/kb/004-talos-patch-rollout-gotchas-tuppr.md).
