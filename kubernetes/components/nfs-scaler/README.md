# NFS Scaler Component

This component uses KEDA (Kubernetes Event Driven Autoscaling) to automatically scale deployments to zero when NFS storage is unavailable, preventing pod crashes and CrashLoopBackOff states.

## How It Works

The NFS scaler component:

1. **Monitors NFS availability** via Prometheus blackbox-exporter probes on port 2049 (NFS)
2. **Scales deployments to 0** when NFS is unavailable (probe_success = 0)
3. **Scales deployments to 1** when NFS becomes available (probe_success = 1)
4. **Prevents application failures** by proactively shutting down pods that would otherwise crash trying to mount unavailable NFS shares

## Prerequisites

- KEDA installed in the cluster (see `kubernetes/apps/kube-system/keda`)
- Prometheus operator for metrics
- Blackbox-exporter configured to probe NFS endpoints on port 2049 (see `kubernetes/apps/observability/blackbox-exporter/lan/probes.yaml`)

## Usage

Add the component to your application's kustomization.yaml:

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
components:
  - ../../../../components/nfs-scaler
resources:
  - ./helmrelease.yaml
replacements:
  - source:
      kind: HelmRelease
      name: your-app-name
      fieldPath: metadata.name
    targets:
      - select:
          kind: ScaledObject
        fieldPaths:
          - metadata.name
          - spec.scaleTargetRef.name
```

The replacements section automatically configures the ScaledObject to target your deployment using the HelmRelease name.

## Applications Using This Component

The following applications use the nfs-scaler component:

### Media Apps
- Plex
- Jellyfin
- Sonarr
- Radarr
- Radarr4K
- Bazarr
- Pinchflat

### Download Clients
- qBittorrent
- SABnzbd
- NZBGet

## Configuration

The ScaledObject is configured with:

- **minReplicaCount**: 0 (scales to zero when NFS unavailable)
- **maxReplicaCount**: 1 (scales back to 1 when NFS available)
- **cooldownPeriod**: 0 (immediate response to changes)
- **Prometheus query**: `probe_success{instance=~".+:2049"}`
- **Threshold**: 1 (scales up when probe succeeds)

## Benefits

1. **Prevents CrashLoopBackOff**: Applications don't repeatedly fail trying to mount unavailable NFS shares
2. **Automatic recovery**: When NFS comes back online, applications automatically scale back up
3. **Homelab-friendly**: Perfect for environments where NFS storage might be on separate hardware that could be powered down
4. **Resource efficient**: No wasted resources on crashing pods

## Inspired By

This implementation is based on [onedr0p/home-ops](https://github.com/onedr0p/home-ops/tree/main/kubernetes/components/nfs-scaler) NFS scaler pattern.
