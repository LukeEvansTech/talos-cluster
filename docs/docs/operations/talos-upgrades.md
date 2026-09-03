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

### Upgrade "didn't take": node reboots into the old version

**Symptom:** tuppr sits in `Rebooting` for ~7 minutes, the node comes back Ready on the _old_
version, tuppr reports `version mismatch`, `LoaderEntryDefault` points at the old UKI while the new
`Talos-vX.efi` is on the ESP, and `talosctl upgrade --wait` still exits 0. The new version never
booted, so do not debug it as a boot failure.

**Cause (1.13.9 source):** the upgrade API installs first, then runs the reboot sequence. Its
`volumeFinalize` / `teardownLifecycle` step has to close the LUKS `EPHEMERAL` volume; if anything
still references `/dev/dm-1` it retries `mapped device is still in use` for 5 minutes, the sequence
errors, and machined's fatal-error handler calls `revertBootloader()`, which puts the old UKI back
as the sd-boot default before rebooting. On this cluster the holder is miroir: its loopfile
volumes under `/var/mnt/extra/miroir/volumes/` stay attached as loop devices (one node had 196,
most of them leaked), and the miroir agent re-attaches them within ~30 seconds of a `losetup -d`
even on a drained node. `talosctl upgrade --stage` no longer exists in the 1.13 or 1.14 client.

**Procedure that works (per node):**

1. `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`.
2. Keep the agent off the node (it tolerates every taint):
   `kubectl -n miroir-system patch ds miroir-agent --type=merge -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"kubernetes.io/hostname","operator":"NotIn","values":["<node>"]}]}]}}}}}}}'`
   and wait for its pod on that node to disappear.
3. From a privileged `hostPID` pod on the node, `nsenter --mount=/host/proc/$(pidof kubelet)/ns/mnt`
   (the kubelet image carries util-linux, like the fstrim CronJob does) and run
   `umount` on every remaining `.../csi/miroir.home-operations.com/*/globalmount`, then
   `losetup -d` on every loop whose backing file is under `/var/`.
4. Confirm from the host that no loop is backed by `/var` for ~40 seconds, then
   `talosctl upgrade --image factory.talos.dev/installer-secureboot/<schematic>:<version> --wait`.
   Teardown then takes seconds.
5. After the node is back: `kubectl -n miroir-system patch ds miroir-agent --type=json -p '[{"op":"remove","path":"/spec/template/spec/affinity"}]'`,
   `kubectl uncordon <node>`, and delete + let Flux recreate the tuppr `TalosUpgrade` so it shows
   `Completed`.

If the new UKI is already on the ESP and the teardown still stalls, `talosctl rollback` is a
rollback-_forward_: sd-boot's `Revert()` points the default at the other UKI and reboots, and with
no fallback tag left in META the failure-path revert is a no-op. That boot has no auto-revert.

To see what the shutdown is doing, add a `KmsgLogConfig` document (`url: udp://<other node>:5514`)
pointing at a `hostNetwork` socat pod on another node; the delivery starts on apply and only changes
target on delete + re-add. Loop attach/detach show up as `loopN: detected capacity change`.

### Applying a regenerated config: check the etcd encryption key name

talhelper 3.1.17 (machinery `v1.14.0-alpha.2`) renders `KubeEtcdEncryptionConfig` with the secretbox
key named `key1`. Talos has always rendered the v1alpha1 `secretboxEncryptionSecret` as `key2`, and
etcd ciphertext is prefixed with the key name, so applying that render stops every kube-apiserver
from decrypting Secrets (`no matching key was found for the provided Secretbox transformer`) and
every controller that reads Secrets crash-loops. `talos/patches/controller/cluster.yaml` deletes the
generated document and `talos/patches/controller/etcd-encryption.yaml` restates it with `key2`; the
value comes from the talsecret document via `TALOS_SECRETBOX_SECRET`, exported by
`just talos gen-config`, which also refuses any render that does not carry exactly one `key2`.
Before applying any regenerated config, save the live one
(`talosctl -n <ip> get mc v1alpha1 -o yaml`, the config is the `.spec` string) and dry-run first;
re-applying that saved file is the recovery.
