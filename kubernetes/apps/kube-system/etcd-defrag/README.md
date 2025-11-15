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

### 1Password Secret

The CronJob requires a `talosconfig` file to authenticate with Talos nodes. This must be stored in 1Password.

**Setup Steps:**

1. Read your talosconfig file:
   ```bash
   cat talos/clusterconfig/talosconfig
   ```

2. Add to 1Password:
   - Navigate to your "Talos" vault in 1Password
   - Find or create an item named "talos"
   - Add a new field named `TALOSCONFIG`
   - Paste the entire talosconfig file content

The ExternalSecret will automatically sync this to Kubernetes as `etcd-defrag-talosconfig` in the `kube-system` namespace.

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

Ensure the `TALOSCONFIG` field exists in 1Password under `talos` vault.

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
