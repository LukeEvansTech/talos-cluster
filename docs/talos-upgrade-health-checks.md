# Talos Upgrade Health Check Troubleshooting

This document covers common issues that can block Talos upgrades when using health checks.

## Overview

The Talos upgrade plan uses health checks to ensure the cluster is in a healthy state before proceeding with node upgrades. If any health check fails, the plan will stall waiting for the condition to be met.

## Configured Health Checks

```yaml
healthChecks:
  - apiVersion: volsync.backube/v1alpha1
    expr: status.conditions.filter(c, c.type == "Synchronizing").all(c, c.status == "False")
    kind: ReplicationSource
  - apiVersion: ceph.rook.io/v1
    expr: status.ceph.health in ['HEALTH_OK']
    kind: CephCluster
```

### 1. VolSync ReplicationSource Check

**Requirement:** All ReplicationSource resources must have `Synchronizing=False`

**What it checks:** Ensures no active backup/replication jobs are in progress before rebooting nodes.

**Troubleshooting:**
```bash
# Check if any ReplicationSources are actively syncing
kubectl get replicationsource -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: Synchronizing={.status.conditions[?(@.type=="Synchronizing")].status}{"\n"}{end}' | grep -v "False"
```

If backups are in progress, either wait for them to complete or check for stuck jobs.

### 2. CephCluster Health Check

**Requirement:** CephCluster must report `HEALTH_OK`

**What it checks:** Ensures Ceph storage is fully healthy before proceeding with node operations.

**Troubleshooting:**
```bash
# Check current Ceph health
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health

# Get detailed status
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status
```

## Common Issues

### Ceph HEALTH_WARN: MGR Module Crashes

**Symptom:** Plan stalls with Ceph reporting `HEALTH_WARN` due to unacknowledged crash reports.

```
health: HEALTH_WARN
        9 mgr modules have recently crashed
```

**Cause:** The Ceph manager (mgr) has experienced crashes that haven't been acknowledged. These are often transient issues that have already self-resolved, but Ceph keeps the crash reports until manually archived.

**Resolution:**
```bash
# List crash reports
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph crash ls

# Archive all crash reports to clear the warning
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph crash archive-all

# Verify health is now OK
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health
```

### Ceph HEALTH_WARN: Other Causes

Other common causes of `HEALTH_WARN`:

| Warning | Cause | Resolution |
|---------|-------|------------|
| `clock skew detected` | NTP sync issues between nodes | Fix time synchronization |
| `osds are down` | OSD pods not running | Check OSD pod status and logs |
| `pgs degraded` | Data not fully replicated | Wait for rebalancing or investigate failed OSDs |
| `pool has no application` | Pool misconfiguration | Set pool application: `ceph osd pool application enable <pool> <app>` |

```bash
# Get detailed health information
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health detail
```

### VolSync Stuck Synchronizing

**Symptom:** ReplicationSource stuck in `Synchronizing=True`

**Resolution:**
```bash
# Find the stuck replication source
kubectl get replicationsource -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: Synchronizing={.status.conditions[?(@.type=="Synchronizing")].status}{"\n"}{end}' | grep "True"

# Check the replication source status
kubectl describe replicationsource <name> -n <namespace>

# Check the associated job/pod
kubectl get pods -n <namespace> | grep <replicationsource-name>
```

## Monitoring Upgrade Progress

```bash
# Watch system-upgrade pods
kubectl get pods -n system-upgrade -w

# Check upgrade logs
kubectl logs -n system-upgrade -l upgrade.cattle.io/plan=talos -f

# Check node versions
kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage
```

## Forcing Upgrade (Use with Caution)

If you need to bypass health checks temporarily, you can modify the plan policy:

```yaml
policy:
  force: true  # Bypasses health checks - use with caution!
```

This is **not recommended** for production use as it may cause data loss or service disruption.
