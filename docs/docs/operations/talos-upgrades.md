# Talos upgrades

How tuppr upgrades Talos on this cluster, what tends to block it, and what changed when the fleet
moved to 1.14.

## Health checks

tuppr runs these checks before it touches each node. If one fails, the plan stalls until the
condition clears.

```yaml
healthChecks:
  - apiVersion: volsync.backube/v1alpha1
    expr: status.conditions.filter(c, c.type == "Synchronizing").all(c, c.status == "False")
    kind: ReplicationSource
  - apiVersion: ceph.rook.io/v1
    expr: status.ceph.health in ['HEALTH_OK']
    kind: CephCluster
```

### VolSync ReplicationSource

Every ReplicationSource must report `Synchronizing=False`, so no backup is mid-run when a node
reboots. To list the ones still syncing:

```bash
kubectl get replicationsource -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: Synchronizing={.status.conditions[?(@.type=="Synchronizing")].status}{"\n"}{end}' | grep -v "False"
```

Wait for them to finish, or look for a stuck job (see below).

### CephCluster health

The CephCluster must report `HEALTH_OK`.

```bash
# Current health
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health

# Full status
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status
```

## Common blockers

### Ceph HEALTH_WARN from mgr crash reports

The plan stalls with Ceph reporting `HEALTH_WARN` because of crash reports nobody has archived:

```text
health: HEALTH_WARN
        9 mgr modules have recently crashed
```

The mgr crashed at some point and recovered on its own, but Ceph keeps the warning until the
reports are archived:

```bash
# List crash reports
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph crash ls

# Archive them all to clear the warning
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph crash archive-all

# Confirm health is back to OK
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health
```

### Other Ceph warnings

| Warning                   | Cause                         | Resolution                                                            |
| ------------------------- | ----------------------------- | --------------------------------------------------------------------- |
| `clock skew detected`     | NTP sync issues between nodes | Fix time synchronization                                              |
| `osds are down`           | OSD pods not running          | Check OSD pod status and logs                                         |
| `pgs degraded`            | Data not fully replicated     | Wait for rebalancing or investigate failed OSDs                       |
| `pool has no application` | Pool misconfiguration         | Set pool application: `ceph osd pool application enable <pool> <app>` |

```bash
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health detail
```

### A ReplicationSource stuck synchronizing

If a ReplicationSource stays at `Synchronizing=True`:

```bash
# Find it
kubectl get replicationsource -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: Synchronizing={.status.conditions[?(@.type=="Synchronizing")].status}{"\n"}{end}' | grep "True"

# Read its status
kubectl describe replicationsource <name> -n <namespace>

# Find the job or pod behind it
kubectl get pods -n <namespace> | grep <replicationsource-name>
```

### `just talos gen-config` fails: "is not a supported Talos version"

Right after a Talos minor bump (v1.12.x to v1.13.x, say), `just talos gen-config` fails with:

```text
field: "talosVersion"
  * "vX.Y.Z" is not a supported Talos version
```

talhelper carries a hardcoded list of supported Talos versions through its
`github.com/siderolabs/talos/pkg/machinery` dependency. A new Talos minor needs a talhelper release
that bumps that dependency, and that release is usually a day or two behind Talos GA. Patch bumps
within a minor (v1.13.0 to v1.13.1) do not need a talhelper update.

What to do about it:

- A version-bump-only upgrade still works. tuppr drives `TalosUpgrade` and `KubernetesUpgrade`
  without talhelper, so you do not need `gen-config` to roll the fleet to the new version.
- If you need a machine-config regen before talhelper catches up (an apiserver flag or a
  feature-gate change, for instance), edit each control plane out of band with a scripted `EDITOR`:

  ```bash
  talosctl edit machineconfig --nodes <node-ip>
  ```

- Watch for a matching release at <https://github.com/budimanjojo/talhelper/releases>, then return
  to the normal `just talos gen-config` flow.
- A minor bump can also change the config contract, which is a different failure: with Talos 1.14
  talhelper accepted the version but rejected the old v1alpha1 patches. See
  [Talos 1.14 notes](#talos-114-notes).

### Image verification failures

An upgrade (or any pull of a Sidero image) fails with:

```text
image verification failed: no valid signature found
```

Every node cosign-verifies `ghcr.io/siderolabs/*` and `factory.talos.dev/*` images at pull time.
See [image verification](../architecture/image-verification.md) for the rules and identities.
Upgrades pull the factory installer image through this check, so a verification failure blocks the
upgrade before anything is written to disk.

Work through it in this order:

1. Verify the exact image by hand and compare the signing identity against the rule:

   ```bash
   mise exec "aqua:sigstore/cosign@latest" -- cosign verify \
     --certificate-oidc-issuer=https://accounts.google.com \
     --certificate-identity=image-factory-signing@talos-production.iam.gserviceaccount.com \
     "factory.talos.dev/installer-secureboot/<schematic>:<version>"
   ```

2. If cosign fails too, Sidero has probably rotated its signing identity or changed the signature
   format. Check recent `siderolabs/talos` and `siderolabs/image-factory` releases, update the
   identities in `talos/patches/global/machine-image-verification.yaml`, regenerate and re-apply.
3. If cosign succeeds but the node still rejects the image, suspect the Talos-side verifier. The
   OCI-referrers format bug (siderolabs/talos#13639) is the known class of failure.

To bypass the check in an emergency, drop the `machine-image-verification.yaml` line from the
`patches` list in `talos/talconfig.yaml`, run `just talos gen-config`, apply to the affected node
(no reboot), and restore it after the upgrade.

## Watching an upgrade

```bash
# Upgrade pods
kubectl get pods -n system-upgrade -w

# tuppr upgrade jobs
kubectl get jobs -n system-upgrade

# tuppr controller logs
kubectl logs -n system-upgrade -l app.kubernetes.io/name=tuppr -f

# Node versions
kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage
```

## Bypassing a blocked upgrade

tuppr's `TalosUpgrade` spec has no `force` field. Either clear the blocking condition (archive the
Ceph crash reports, wait for VolSync to finish, fix whatever the health check is reporting) or
upgrade the node out of band with `talosctl`, which skips tuppr entirely.

For the manual route, prefer the existing `just talos upgrade-node <node-ip>` recipe in
`talos/mod.just`. It derives the installer image and version from `talconfig.yaml` and
`talenv.yaml` through talhelper, so you do not have to assemble the flags yourself. The command it
wraps is:

```bash
talosctl upgrade --nodes <node-ip> --image <talos-installer-image>:<version> --timeout=10m
```

Replace `<node-ip>` and `<version>` with the target node address and the Talos version from
`talos/talconfig.yaml`.

Only do this when you know why the check is failing. The check exists to stop a reboot from making
that problem worse.

## Talos 1.14 notes

What changed for this cluster when the fleet moved from 1.13 to 1.14 in September 2026, and what
is still pending.

The installer image now comes from the Image Factory. Sidero does not publish
`ghcr.io/siderolabs/installer` for 1.14 or later, so tuppr must be 0.5.3 or newer (it builds the
`installer-secureboot` image from each node's own schematic), and the Renovate pins in
`talconfig.yaml`, `talenv.yaml` and the `TalosUpgrade` use
`datasource=custom.talos-factory depName=siderolabs/talos` (the home-operations `talosFactory`
preset) instead of the Docker installer tag.

The upgrade is one way. 1.14 ships etcd 3.7 and 1.13 only supports etcd 3.6, so `talosctl rollback`
to 1.13 stops being an option once a node has booted 1.14.

The machine config is a hybrid until talhelper catches up. With `talosVersion: v1.14.0` talhelper
renders the 1.14 contract: kube-apiserver, controller-manager, scheduler, kube-proxy, admission
control, cluster network, etcd encryption and discovery become multi-document kinds, and it rejects
the old v1alpha1 patches for them (`cluster proxy config in v1alpha1 config ... can't be used with
KubeProxyConfig document`). talhelper 3.1.17 (machinery `v1.14.0-alpha.2`) does not know
`KubeNodeConfig`, `KubeCoreDNSConfig`, `KubeTalosAPIAccessConfig`, `KernelModuleConfig`,
`CRICustomizationConfig` or `encryption.allowDiscards`, and still writes the kubelet into v1alpha1,
so those settings stay v1alpha1 (deprecated, still supported) with a note in each patch. Finish the
migration when talhelper ships GA 1.14 machinery. The generator also drops
`additionalApiServerCertSans` in the 1.14 contract, so the apiserver SANs are restated as
`certExtraSANs` in `talos/patches/controller/cluster.yaml`. Keep both lists in sync.

The rendered config only applies to a 1.14 node, because a 1.13 node rejects the new document
kinds. Roll the fleet with tuppr first, then run `just talos gen-config` and apply per node, dry
run first:

```bash
talosctl apply-config --dry-run --nodes <node-ip> --file talos/clusterconfig/<node>.yaml
```

`--mode=reboot` was removed from `apply-config` in 1.14; reboot explicitly if the diff needs it.

1.14 adds `FilesystemTrimConfig` (`talos/patches/global/machine-fstrim.yaml`), which trims only
the volumes Talos mounts itself. The `kube-system/fstrim` CronJob stays, because it is what trims
the Ceph RBD PVC filesystems and the miroir loop devices. Trimming the LUKS-encrypted `EPHEMERAL`
volume also needs `encryption.allowDiscards: true`, which waits for talhelper support.

etcd metrics stay on port 2381 because `listen-metrics-urls` is set. Stock 1.14 moves the HTTP
endpoints to 2383.

### Upgrade "didn't take": node reboots into the old version

tuppr sits in `Rebooting` for about 7 minutes, the node comes back Ready on the old version, tuppr
reports `version mismatch`, `LoaderEntryDefault` points at the old UKI while the new `Talos-vX.efi`
is on the ESP, and `talosctl upgrade --wait` still exits 0. The new version never booted, so do not
debug it as a boot failure.

[KB-028](../troubleshooting/kb/028-talos-upgrade-boots-old-version-loaderentrydefault.md) describes
the same outward symptom with a different recovery (delete `LoaderEntryDefault`, do not re-run the
upgrade). Tell them apart before choosing. This case shows a 5-minute gap between tuppr entering
`Rebooting` (or `talosctl upgrade` printing `installation ... complete`) and the node going down,
and the machined log of that shutdown (stream it with the `KmsgLogConfig` trick below) ends with
`task teardownLifecycle (1/1): failed: context deadline exceeded`, `running emergency volume
cleanup`, and thousands of `error closing luks2-EPHEMERAL: mapped device is still in use` lines. A
prompt reboot with none of that is KB-028. Either recovery leaves the node on the new version; the
loop-device cleanup below is what stops it recurring on the next upgrade.

The cause, read from the 1.13.9 source: the upgrade API installs first, then runs the reboot
sequence. Its `volumeFinalize` / `teardownLifecycle` step has to close the LUKS `EPHEMERAL` volume.
If anything still references `/dev/dm-1` it retries `mapped device is still in use` for 5 minutes,
the sequence errors, and machined's fatal-error handler calls `revertBootloader()`, which puts the
old UKI back as the sd-boot default before rebooting. On this cluster the holder is miroir. Its
loopfile volumes under `/var/mnt/extra/miroir/volumes/` stay attached as loop devices (one node had
196, most of them leaked), and the miroir agent re-attaches them within about 30 seconds of a
`losetup -d`, even on a drained node. `talosctl upgrade --stage` no longer exists in the 1.13 or
1.14 client.

The procedure that works, per node:

1. `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`.
2. Keep the agent off the node (it tolerates every taint):
   `kubectl -n miroir-system patch ds miroir-agent --type=merge -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"kubernetes.io/hostname","operator":"NotIn","values":["<node>"]}]}]}}}}}}}'`
   and wait for its pod on that node to disappear.
3. From a privileged pod on the node with `hostPID: true` and a `hostPath` mount of `/proc` at
   `/host/proc` (the same shape as the fstrim CronJob in `kubernetes/apps/kube-system/fstrim`),
   run `nsenter --mount=/host/proc/$(pidof kubelet)/ns/mnt` (the kubelet image carries util-linux),
   then `umount` every remaining `.../csi/miroir.home-operations.com/*/globalmount` and
   `losetup -d` every loop whose backing file is under `/var/`.
4. Confirm from the host that no loop is backed by `/var` for about 40 seconds, then
   `talosctl upgrade --nodes <node-ip> --image factory.talos.dev/installer-secureboot/<schematic>:<version> --wait`.
   Teardown then takes seconds.
5. After the node is back: `kubectl -n miroir-system patch ds miroir-agent --type=json -p '[{"op":"remove","path":"/spec/template/spec/affinity"}]'`
   and `kubectl uncordon <node>`.
6. Before touching the next node, wait for Ceph `HEALTH_OK` (`ceph health` in the toolbox) and for
   all three etcd members to be healthy: `talosctl -n <ip-1>,<ip-2>,<ip-3> etcd status` with every
   control-plane address. Each address reports only that node's member, so querying just the node
   you recovered can hide a member that is still down. Kubernetes `Ready` comes well before Ceph and
   etcd have re-converged, and the next control-plane reboot is a quorum risk until they have. Only
   the Ceph half of this gate is automated: the `healthChecks` in the `TalosUpgrade` cover VolSync
   and the `CephCluster`, not etcd, so the etcd check is manual here.
7. When every node is done, delete the tuppr `TalosUpgrade` and let Flux recreate it so it shows
   `Completed`.

If the new UKI is already on the ESP and the teardown still stalls, `talosctl rollback` is a
rollback forward: sd-boot's `Revert()` points the default at the other UKI and reboots, and with
no fallback tag left in META the failure-path revert is a no-op. That boot has no auto-revert.

To see what the shutdown is doing, add a `KmsgLogConfig` document (`url: udp://<other node>:5514`)
pointing at a `hostNetwork` socat pod on another node. Delivery starts on apply and only changes
target on delete plus re-add. Loop attach and detach show up as `loopN: detected capacity change`.

### Applying a regenerated config: check the etcd encryption key name

talhelper 3.1.17 (machinery `v1.14.0-alpha.2`) renders `KubeEtcdEncryptionConfig` with the secretbox
key named `key1`. Talos has always rendered the v1alpha1 `secretboxEncryptionSecret` as `key2`, and
etcd ciphertext is prefixed with the key name, so applying that render stops every kube-apiserver
from decrypting Secrets (`no matching key was found for the provided Secretbox transformer`) and
every controller that reads Secrets crash-loops. `talos/patches/controller/cluster.yaml` deletes the
generated document and `talos/patches/controller/etcd-encryption.yaml` restates it with `key2`. The
value comes from the talsecret document through `TALOS_SECRETBOX_SECRET`, exported by
`just talos gen-config`, which also refuses any render that does not carry exactly one `key2`.
Before applying any regenerated config, save the live one
(`talosctl -n <ip> get mc v1alpha1 -o yaml`; the config is the `.spec` string) and dry-run first.
Re-applying that saved file is the recovery.
