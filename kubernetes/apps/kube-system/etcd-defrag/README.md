# etcd Defragmentation CronJob

Automated defragmentation for etcd cluster running on Talos Linux control plane nodes.

## Overview

This CronJob runs weekly to defragment the etcd database on all control plane nodes, preventing fragmentation issues and recovering unused disk space.

## Features

- **Safe Sequential Defragmentation**: Defrags one node at a time to maintain quorum
- **Weekly Schedule**: Runs Sunday at 2 AM UTC
- **Health Checks**: Verifies cluster health before and after defragmentation
- **Detailed Logging**: Shows defragmentation progress and database size changes

## Prerequisites

### 1Password Secrets (Certificate Components)

The CronJob requires Talos client certificates to authenticate with Talos nodes. For security, we store only the individual certificate components (not the full talosconfig).

**Setup Steps:**

1. Extract certificate components from your talosconfig:
   ```bash
   # Read your talosconfig
   cat talos/clusterconfig/talosconfig
   ```

2. Add to 1Password in the "Talos" vault, item "talos":
   - Add field `TALOS_CA`: The base64-encoded CA certificate (value of `contexts.kubernetes.ca`)
   - Add field `TALOS_CRT`: The base64-encoded client certificate (value of `contexts.kubernetes.crt`)
   - Add field `TALOS_KEY`: The base64-encoded client key (value of `contexts.kubernetes.key`)

**Example talosconfig structure:**
```yaml
context: kubernetes
contexts:
  kubernetes:
    endpoints: [...]
    ca: <copy this value to TALOS_CA>
    crt: <copy this value to TALOS_CRT>
    key: <copy this value to TALOS_KEY>
```

The ExternalSecret will automatically sync these components to Kubernetes as `etcd-defrag-talosconfig` in the `kube-system` namespace. The defragmentation script builds a minimal talosconfig at runtime from these components.

**Security Note:** This approach follows the principle of least privilege by storing only the necessary certificate components instead of the full talosconfig file.

## Manual Defragmentation

To defragment immediately without waiting for the CronJob:

```bash
# Check current status
talosctl -n 10.32.8.80,10.32.8.81,10.32.8.82 service etcd status

# Defragment each node sequentially
talosctl -n 10.32.8.80 service etcd defragment  # cr-talos-01
talosctl -n 10.32.8.81 service etcd defragment  # cr-talos-02
talosctl -n 10.32.8.82 service etcd defragment  # cr-talos-03

# Verify completion
talosctl -n 10.32.8.80,10.32.8.81,10.32.8.82 service etcd status
```

## Monitoring

Monitor the CronJob execution:

```bash
# View CronJob status
kubectl get cronjob -n kube-system etcd-defrag

# View job history
kubectl get jobs -n kube-system -l app.kubernetes.io/name=etcd-defrag

# View logs from latest run
kubectl logs -n kube-system -l app.kubernetes.io/name=etcd-defrag --tail=100
```

## Schedule Modification

To change the defragmentation schedule, edit `app/helmrelease.yaml`:

```yaml
cronjob:
  schedule: "0 2 * * 0"  # Cron format: minute hour day month weekday
```

**Examples:**
- Daily: `"0 2 * * *"`
- Bi-weekly: `"0 2 */14 * *"`
- Monthly: `"0 2 1 * *"`

## Troubleshooting

### CronJob not running

Check Flux reconciliation:
```bash
kubectl get kustomization -n flux-system etcd-defrag
flux reconcile kustomization etcd-defrag
```

### Secret not found

Verify the ExternalSecret is syncing:
```bash
kubectl get externalsecret -n kube-system etcd-defrag
kubectl describe externalsecret -n kube-system etcd-defrag
```

Ensure these fields exist in 1Password under the "talos" vault, item "talos":
- `TALOS_CA`
- `TALOS_CRT`
- `TALOS_KEY`

### Defragmentation fails

1. Check etcd cluster health:
   ```bash
   talosctl -n 10.32.8.80 service etcd status
   ```

2. Ensure all control plane nodes are healthy
3. Verify quorum (at least 2 of 3 nodes must be available)

## References

- [Talos etcd Maintenance Documentation](https://www.talos.dev/v1.11/advanced/etcd-maintenance/)
- [etcd Defragmentation Guide](https://etcd.io/docs/latest/op-guide/maintenance/#defragmentation)
