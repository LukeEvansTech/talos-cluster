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

```text
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

| Warning                   | Cause                         | Resolution                                                            |
| ------------------------- | ----------------------------- | --------------------------------------------------------------------- |
| `clock skew detected`     | NTP sync issues between nodes | Fix time synchronization                                              |
| `osds are down`           | OSD pods not running          | Check OSD pod status and logs                                         |
| `pgs degraded`            | Data not fully replicated     | Wait for rebalancing or investigate failed OSDs                       |
| `pool has no application` | Pool misconfiguration         | Set pool application: `ceph osd pool application enable <pool> <app>` |

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

### `just talos gen-config` fails: "is not a supported Talos version"

**Symptom:** right after a Talos **minor** bump (e.g. v1.12.x → v1.13.x), `just talos gen-config`
errors:

```text
field: "talosVersion"
  * "vX.Y.Z" is not a supported Talos version
```

**Cause:** talhelper carries a hardcoded list of supported Talos versions via its
`github.com/siderolabs/talos/pkg/machinery` dependency. A new Talos **minor** needs a talhelper
release that bumps that dependency, which typically lags Talos GA by **1-2 days**. (Patch bumps
within a minor, v1.13.0 → v1.13.1, don't need a talhelper update.)

**Resolution:**

- A **version-bump-only** upgrade still works: TUPPR drives `TalosUpgrade` / `KubernetesUpgrade`
  independently of talhelper, so you don't need `gen-config` to roll the fleet to the new version.
- If you need a **machine-config regen** before talhelper catches up (e.g. an apiserver flag or
  feature-gate change), edit each control plane out-of-band with a scripted `EDITOR`:

  ```bash
  talosctl edit machineconfig --nodes <node-ip>
  ```

- Watch for a matching release at <https://github.com/budimanjojo/talhelper/releases>, then return
  to the normal `just talos gen-config` flow.
- A minor bump can also change the **config contract**, which is a different failure: with
  Talos 1.14 talhelper accepted the version but rejected the old v1alpha1 patches. See
  [Talos 1.14 notes](#talos-114-notes).

### Image verification failures

**Symptom:** an upgrade (or any image pull of a Sidero image) fails with:

```text
image verification failed: no valid signature found
```

Every node cosign-verifies `ghcr.io/siderolabs/*` and `factory.talos.dev/*` images at pull time.
See [image verification](../architecture/image-verification.md) for the rules and identities.
Upgrades pull the factory installer image through this check, so a verification failure blocks the
upgrade before anything is written to disk.

**Diagnosis order:**

1. Verify the exact image manually and compare the signing identity against the rule:

   ```bash
   mise exec "aqua:sigstore/cosign@latest" -- cosign verify \
     --certificate-oidc-issuer=https://accounts.google.com \
     --certificate-identity=image-factory-signing@talos-production.iam.gserviceaccount.com \
     "factory.talos.dev/installer-secureboot/<schematic>:<version>"
   ```

2. If cosign fails too, Sidero likely rotated its signing identity or changed signature format:
   check recent `siderolabs/talos` and `siderolabs/image-factory` releases, update the identities
   in `talos/patches/global/machine-image-verification.yaml`, regenerate and re-apply.
3. If cosign succeeds but the node still rejects, suspect the Talos-side verifier (the
   OCI-referrers format bug, siderolabs/talos#13639, is the known class of failure).

**Emergency bypass:** drop the `machine-image-verification.yaml` line from the `patches` list in
`talos/talconfig.yaml`, run `just talos gen-config`, apply to the affected node (no reboot), and
restore it after the upgrade.

## Monitoring Upgrade Progress

```bash
# Watch system-upgrade pods
kubectl get pods -n system-upgrade -w

# Check tuppr upgrade jobs
kubectl get jobs -n system-upgrade

# Check tuppr controller logs
kubectl logs -n system-upgrade -l app.kubernetes.io/name=tuppr -f

# Check node versions
kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage
```

## Bypassing a Blocked Upgrade (Use with Caution)

Tuppr's `TalosUpgrade` spec does not have a `force` field. To bypass a blocked upgrade, address the blocking condition directly or use `talosctl` to upgrade a node out-of-band:

1. **Fix the blocking condition**: archive Ceph crash reports, wait for VolSync to finish, or resolve the health check failure.
2. **Manual per-node upgrade**: bypasses tuppr entirely. Prefer the existing `just talos upgrade-node <node-ip>` recipe (`talos/mod.just`): it derives the correct installer image and version from `talconfig.yaml`/`talenv.yaml` via talhelper, so you don't have to hand-assemble the flags. The raw form it wraps is:

   ```bash
   talosctl upgrade --nodes <node-ip> --image <talos-installer-image>:<version> --timeout=10m
   ```

   Replace `<node-ip>` and `<version>` with the target node address and Talos version from `talos/talconfig.yaml`.

This is **not recommended** for production use unless the cluster health is understood. Always resolve the underlying issue before proceeding.

## Talos 1.14 notes

What changed for this cluster when the fleet moved from 1.13 to 1.14 (September 2026), and what is still
pending.

- **Installer image moved to the Image Factory.** `ghcr.io/siderolabs/installer` is not published
  for 1.14+. tuppr 0.5.3 or newer is required (it builds the `installer-secureboot` image from each
  node's own schematic), and the Renovate pins in `talconfig.yaml`, `talenv.yaml` and the
  `TalosUpgrade` use `datasource=custom.talos-factory depName=siderolabs/talos` (the home-operations
  `talosFactory` preset) instead of the Docker installer tag.
- **The upgrade is one-way.** 1.14 ships etcd 3.7 and 1.13 only supports etcd 3.6, so
  `talosctl rollback` to 1.13 is not an option once a node has booted 1.14.
- **Machine config is a hybrid until talhelper catches up.** With `talosVersion: v1.14.0` talhelper
  renders the 1.14 contract: kube-apiserver, controller-manager, scheduler, kube-proxy, admission
  control, cluster network, etcd encryption and discovery are multi-document kinds, and the old
  v1alpha1 patches for them are rejected (`cluster proxy config in v1alpha1 config ... can't be used
with KubeProxyConfig document`). talhelper 3.1.17 (machinery `v1.14.0-alpha.2`) does not know
  `KubeNodeConfig`, `KubeCoreDNSConfig`, `KubeTalosAPIAccessConfig`, `KernelModuleConfig`,
  `CRICustomizationConfig` or `encryption.allowDiscards`, and still writes the kubelet into
  v1alpha1, so those settings stay v1alpha1 (deprecated, still supported) with a note in each
  patch. Finish the migration when talhelper ships GA 1.14 machinery. The generator also drops
  `additionalApiServerCertSans` in the 1.14 contract, so the apiserver SANs are restated as
  `certExtraSANs` in `talos/patches/controller/cluster.yaml`; keep both lists in sync.
- **The rendered config only applies to a 1.14 node.** A 1.13 node rejects the new document kinds.
  Roll the fleet with tuppr first, then `just talos gen-config` and apply per node with a dry run:

  ```bash
  talosctl apply-config --dry-run --nodes <node-ip> --file talos/clusterconfig/<node>.yaml
  ```

  `--mode=reboot` was removed from `apply-config` in 1.14; reboot explicitly if the diff needs it.

- **fstrim.** 1.14 adds `FilesystemTrimConfig` (`talos/patches/global/machine-fstrim.yaml`), which
  trims only the volumes Talos mounts itself. The `kube-system/fstrim` CronJob stays because it is
  what trims the Ceph RBD PVC filesystems and the miroir loop devices. Trimming the LUKS-encrypted
  `EPHEMERAL` volume also needs `encryption.allowDiscards: true`, which waits for talhelper support.
- **etcd metrics stay on port 2381** because `listen-metrics-urls` is set; stock 1.14 moves the
  HTTP endpoints to 2383.
