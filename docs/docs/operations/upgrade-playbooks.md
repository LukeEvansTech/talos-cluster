# Upgrade playbooks for protected infrastructure

Per-component knowledge for the set Renovate never auto-merges: the "Protected infra" entry in
`.renovaterc.json5` is the source of truth for membership. [Renovate auto-merge](renovate-automerge.md)
explains the policy; this page is what to do when one of those PRs is in front of you.

Every section has the same shape so it can be worked top to bottom:

- **Arrives as**: how Renovate presents the bump (group name, which datasource, one PR or several).
- **Repo paths**: the files a version change touches, and the values that are deliberate
  deviations a blind revert would drop.
- **Read before merging**: the release notes and the repo-specific history that matter.
- **Known breaking patterns**: what has broken here before, and the edit that goes in the same PR.
- **After merge**: the checks beyond the generic [health verdict](health-verdict.md), and the
  operator-rendered rollouts that need a nudge.
- **Rollback caveats**: where `git revert` is not enough.

Three rules apply to all of them:

1. **Roll back by re-pinning the tag, never by reverting the whole file.** Most of these files
   carry intentional deviations (memory limits, deviations from chart defaults, mutes) that a bulk
   revert silently drops.
2. **An operator that renders its own workloads only re-renders on CR events.** A chart bump that
   only changes a ConfigMap the operator reads by name rolls nothing. Check the running image
   after every such bump; the Rook section is the worked example.
3. **A bump that carries a second, unpinned component is a two-component upgrade.** Flux's
   distribution rides the operator manifests; Ceph's daemon version rides the Rook cluster chart.
   Read both sets of release notes.

## Talos

**Arrives as:** the `Talos` group, from the `custom.talos-factory` datasource (Sidero no longer
publishes a generic installer image for 1.14+; the pins carry `depName=siderolabs/talos`).

**Repo paths:** `talos/talconfig.yaml` (`talosVersion`), `talos/talenv.yaml`, and the
`TalosUpgrade` CR at `kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml` (tuppr
drives the roll). The machine-config patches under `talos/patches/` are the deviations.

**Read before merging:** the Talos release notes for **every** intervening patch, the tuppr
release notes if tuppr moved too, and [Talos upgrades](talos-upgrades.md) in full. A **minor**
bump also needs a talhelper release that knows the new version; that lags GA by a day or two, and
tuppr does not need it, so a version-bump-only upgrade can go first.

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| A completed install reverts to the old UKI because a loop device holds the encrypted `EPHEMERAL` volume open during shutdown teardown | Detach miroir's loop devices with the agent pinned off the node before each node's upgrade. Procedure in [Talos upgrades](talos-upgrades.md#upgrade-didnt-take-node-reboots-into-the-old-version). tuppr reports `version mismatch`; `talosctl upgrade --wait` exits 0 regardless |
| A stale `LoaderEntryDefault` after an NVRAM wipe boots the old version | [KB-028](../troubleshooting/kb/028-talos-upgrade-boots-old-version-loaderentrydefault.md), a different recovery from the row above; tell them apart by whether the shutdown was prompt |
| A minor bump changes the machine-config contract (1.14 moved apiserver, proxy, etcd encryption and others to multi-document kinds) | Roll the fleet with tuppr **first**, then `just talos gen-config`, then `talosctl apply-config --dry-run` per node. A 1.13 node rejects the new kinds |
| talhelper renders `KubeEtcdEncryptionConfig` with the key named `key1`; Talos has always used `key2`, and etcd ciphertext is prefixed with the key name | Every apiserver stops decrypting Secrets. The patch that restates `key2` and the `gen-config` guard exist; **save the live config straight to a `umask 077` file and dry-run before any regenerated apply**. Never print it: the live config carries the etcd encryption key and every cluster credential |
| tuppr's pre-flight health checks stall on a Ceph `HEALTH_WARN` from archived-or-not crash reports, or a VolSync `ReplicationSource` stuck `Synchronizing` | Archive the crashes. For a stuck **R2** source (restic), set `spec.restic.unlock` to a new string and delete its Job; the Kopia mover the **NFS** sources use has no lock and no `unlock` field, so a stuck NFS source is a failing mover ([KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md), [KB-009](../troubleshooting/kb/009-nfs-mount-failures-host-dns-readonly-export.md)). Both in [Talos upgrades](talos-upgrades.md#common-blockers) |
| The leader tuppr pod is on the node being drained, leadership moves mid-job, and the job is marked `Failed` with the node left cordoned and often already upgraded | Check `machined` logs for `installation of <ver> complete`; reboot to finish; uncordon; delete the `TalosUpgrade` CR and let Flux recreate it |
| Image verification rejects the factory installer | [Talos upgrades](talos-upgrades.md#image-verification-failures) |

**After merge:** node `osImage` and kubelet version per node; `ceph health` `HEALTH_OK` and all
three etcd members healthy **before** the next node (only the Ceph half is automated in tuppr's
health checks); miroir agent affinity restored and every node uncordoned; `KubernetesUpgrade`
and `TalosUpgrade` CRs `Completed`.

**Rollback caveats:** a Talos minor with an etcd major inside it is one way (1.14 ships etcd 3.7;
`talosctl rollback` to 1.13 stops being possible once a node boots 1.14). Within a minor,
`talosctl rollback` flips sd-boot to the other UKI. A node that hangs on shutdown with Rook
volumes still mounted needs a BMC reset; never reboot all three at once.

## Kubernetes

**Arrives as:** the `Kubernetes` group (`kubelet`, `kube-apiserver`, `kube-controller-manager`,
`kube-scheduler`, `kube-proxy`).

**Repo paths:** `talos/talconfig.yaml` (`kubernetesVersion`) and the `KubernetesUpgrade` CR at
`kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml`.

**Read before merging:** the Talos support matrix for the running Talos minor (each Talos minor
caps the Kubernetes version it will run; 1.13 capped at 1.36.x and blocked 1.37 until 1.14), the
Kubernetes changelog for removed APIs and feature-gate graduations, and the vendor matrices of
the operators below. gpu-operator is the one that has historically published an explicit
ceiling; the others document "newest tested" and have not broken on a version one ahead.

**Known breaking patterns:** none recorded on this cluster since tuppr took over. A tuppr Job
failure is terminal until the CR is reset; it does not retry.

**After merge:** apiserver and kubelet versions on all nodes; gpu allocatable still 5 per node;
`kubectl top` works; only control-plane static pods restarted. Restarts of operators inside the
upgrade window are reconcile churn, not a chronic fault (see
[known noise](../troubleshooting/known-noise.md#restarts-that-cluster-inside-an-upgrade-window)).

**Rollback caveats:** a Kubernetes minor cannot be downgraded in place with etcd data written by
the newer apiserver. Treat it as one way.

## Cilium

**Arrives as:** the `Cilium` group (chart from `quay.io/cilium/charts`, plus the operator and
agent images).

**Repo paths:** `kubernetes/apps/kube-system/cilium/app/helm/values.yaml` (rendered into the HR
via a ConfigMap generator), `app/networks.yaml` (L2 announcement and LB pools), and
`kubernetes/apps/kube-system/cilium-policies/` (the cluster-wide policies). Deviations from the
upstream template: `loadBalancer.mode: dsr`, `routingMode: native`, `kubeProxyReplacement: true`,
`socketLB.enabled: true` with no `hostNamespaceOnly` (deliberately dropped in #3755 because it
breaks hairpinning under DSR), `l2announcements.enabled: true`.

**Read before merging:** the Cilium upgrade guide for the target minor (it has a per-version
"upgrade notes" section with required pre-flight steps), and
[KB-008](../troubleshooting/kb/008-cilium-cross-node-pod-networking-breaks.md).

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| The agent DaemonSet rolls on any config change (it carries a configmap checksum) and briefly dips apiserver connectivity | Expect the burst in [known noise](../troubleshooting/known-noise.md#operator-managed-restarts-after-a-protected-infra-bump); restart `coder` explicitly, it does not self-heal |
| A quay.io 504 on the chart's OCIRepository fails the Kustomization's health check and cascades through `dependsOn` | Reconcile the source; [KB-007](../troubleshooting/kb/007-flux-not-ready-artifact-failed-alert-storms.md) |
| Stale cross-node BPF state after one node's prolonged outage | Reboot the **other** nodes; [KB-008](../troubleshooting/kb/008-cilium-cross-node-pod-networking-breaks.md) |

**After merge:** pod DNS resolves from a fresh pod; a ClusterIP service answers; every node's
spegel pod is 1/1 (the first thing to go 0/1 when cross-node traffic breaks); `cilium status`
from an agent pod reports all endpoints and no unreachable nodes; the envoy gateways still hold
their LB addresses.

**Rollback caveats:** Cilium supports rolling back one minor. BPF maps survive agent restarts, so
a bad state may need node reboots even after the chart is reverted.

## Rook-Ceph

**Arrives as:** the `Rook-Ceph` group, tracked as `docker` (the charts are OCIRepository-sourced):
`rook-ceph` (operator) and `rook-ceph-cluster` **must move together**. `ceph-csi-drivers` (the
home-operations mirror of the upstream ceph-csi-operator chart) is an independent line.

**Repo paths:** `kubernetes/apps/rook-ceph/rook-ceph/ks.yaml` wires three Kustomizations in
order, `rook-ceph` then `rook-ceph-csi-drivers` then `rook-ceph-cluster`. `app/` is the operator,
`csi-drivers/` the CSI drivers chart, `cluster/helmrelease.yaml` the `CephCluster`, pools,
filesystem, `cephConfig` and health-check mutes. Deviations a bulk revert would drop:
`drivers.rbd.name: rook-ceph.rbd.csi.ceph.com` and `drivers.rbd.snapshotPolicy: volumeSnapshot`
in the drivers chart; `mon_clock_drift_allowed: "0.3"`, `security.cephx.csi.keyType: aes`,
`keyGeneration: 2` and the `AUTH_INSECURE_*` mutes in the cluster chart.

**Read before merging:** the Rook release notes for every intervening minor (Rook supports only
sequential minor upgrades), the Rook upgrade guide, the Ceph release notes for the chart's
**default Ceph image** (nothing pins `cephClusterSpec.cephVersion`, so a cluster-chart major can
carry a Ceph major), and [KB-010](../troubleshooting/kb/010-rook-ceph-v120-csi-driver-split.md).

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| A cluster-chart major silently bumps the default Ceph image a major (Squid to Tentacle rode v1.19 to v1.20) | Confirm the target Ceph is exactly one major up. Ceph majors are one way. Take a VolSync snapshot pass first (`just kube snapshot`) |
| The upstream ceph-csi-operator chart defaults the RBD driver name to the unprefixed `rbd.csi.ceph.com`; every PV here references the prefixed name and `spec.csi.driver` is immutable | Keep `drivers.rbd.name` set. Without it the in-use driver is wound down and nodeplugins fail on a missing ServiceAccount |
| `drivers.rbd.snapshotPolicy` defaults to `none`, which removes the `csi-snapshotter` sidecar; VolSync's `copyMethod: Snapshot` then hangs every backup | Keep `snapshotPolicy: volumeSnapshot`. If the sidecar is re-added after being absent, delete the orphaned `external-snapshotter-leader-*` Lease |
| A cephcsi bump updates the image-set ConfigMap but the `Driver` CRs reference it by name and the operator does not watch it; plugin pods stay on the old image | `kubectl -n rook-ceph rollout restart deploy/ceph-csi-controller-manager` after **every** bump that touches cephcsi; it re-reconciles every Driver and rolls all plugins in about two minutes with no PVC disruption |
| Toolbox `client.admin` crashes from earlier sessions sit as `HEALTH_WARN` | `ceph crash archive-all` |

**After merge:** `ceph -s` `HEALTH_OK` with 6 OSDs up and in; every `*.csi.ceph.com-{ctrl,node}plugin`
pod runs the image-set cephcsi version (the check that the bump actually landed); a new
`ceph-block` PVC binds and mounts; VolSync source snapshots reach `READYTOUSE=true`; both
`ReplicationSource` movers for one app complete.

**Rollback caveats:** re-pin `ref.tag` on the two Rook OCIRepositories only. A Ceph major does not
roll back with the chart. Crossing the v1.20 CSI split in either direction needs the drivers
chart present and Ready first.

## Flux (operator and instance)

**Arrives as:** the `Flux` group: `flux-operator` chart, `flux-instance` chart, the
`flux-operator-manifests` artifact, and the MCP server image.

**Repo paths:** `kubernetes/apps/flux-system/flux-operator/app/helm/values.yaml` and
`kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml`. The instance HR sets
`instance.distribution.artifact` but **not** `distribution.version`, so the chart default `2.x`
resolves to whatever that artifact bundles. The per-controller resource patches (source-controller
at 1 Gi after the merge-burst OOM, CPU limits stripped) are the deviations.

**Read before merging:** the Flux release notes for the version inside the manifests artifact
(`flux pull artifact` both tags and diff `flux-images/VERSION`), not just the operator notes. The
"Update Flux group" PR is a Flux control-plane upgrade in disguise.

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| A merge burst spikes source-controller past its limit; every HelmRelease then fails `Could not load chart` and the git fix cannot self-apply | Break glass: edit the live `FluxInstance` CR (flux-operator reconciles it independently of source-controller), then land the same value in git. Limit is 1 Gi today |
| The MCP server renames tools between versions | No repo change: nothing hardcodes tool names, and the `flux-mcp-write` ClusterRole is the write boundary |

**After merge:** `flux version` shows the expected `distribution:`; the `FluxInstance`
`status.lastAppliedRevision` matches; all four controllers Running with no restarts; `flux get
kustomizations -A` fully Ready.

**Rollback caveats:** the group moves the manifests artifact **and** the `flux-operator` and
`flux-instance` charts together. Re-pin all three (the artifact tag in the instance values, and
both charts' OCIRepository tags), or a regression in the operator or its templates survives an
artifact-only revert. The operator then re-renders the controllers. Flux CRD schema changes are
additive within 2.x.

## cert-manager

**Arrives as:** the `Cert-Manager` group (chart from `quay.io/jetstack/charts`).

**Repo paths:** `kubernetes/apps/cert-manager/cert-manager/app/helm/values.yaml`,
`app/clusterissuer.yaml`, `app/prometheusrule.yaml`, and `tls/` (the wildcard Certificate and the
PushSecret that copies it to 1Password). The cert-manager Kustomization has `wait: true` and a
`healthCheckExprs` on the ClusterIssuer, and it is a **dependency root**: Envoy Gateway, the CNPG
barman plugin and, through CNPG, most apps depend on it.

**Read before merging:** the cert-manager upgrade notes for the minor (they list removed flags
and API versions per release).

**Known breaking patterns:** a quay.io 504 on the OCIRepository fails the health check and
cascades to ~28 dependents ([KB-007](../troubleshooting/kb/007-flux-not-ready-artifact-failed-alert-storms.md)).
Every `cluster-secrets` refresh also cancels an in-flight health check and restarts its 15-minute
clock, which prolongs recovery.

**After merge:** ClusterIssuer `Ready`; the wildcard Certificate `Ready` with an unchanged
`notAfter`; the PushSecret `Synced`; `flux get kustomizations -A` shows nothing waiting on
cert-manager.

**Rollback caveats:** CRD downgrades are not supported; a bad minor is fixed forward.

## external-secrets and onepassword-connect

**Arrives as:** separate PRs. `external-secrets` (chart) and `1password` (connect chart, the
`connect-api` and `connect-sync` images).

**Repo paths:** `kubernetes/apps/external-secrets/external-secrets/app/` (HR and a PDB) and
`kubernetes/apps/external-secrets/onepassword-connect/app/` (HR, the `ClusterSecretStore`
`onepassword-connect`, and the ExternalSecret holding the Connect credentials). House rule is that
an app with an ExternalSecret `dependsOn` `onepassword-connect`; about half of them do today, so a
broken store blocks those at the Flux layer and leaves the rest waiting on Secrets that never
arrive. Either way nothing with a secret deploys.

**Read before merging:** the external-secrets release notes for API version promotions (every
ExternalSecret and the store are on `external-secrets.io/v1`; a future API promotion means editing
all of them in the same PR), and the Connect server changelog for token-format changes.

**Known breaking patterns:** none recorded on this cluster. The nearest was
[KB-001](../troubleshooting/kb/001-1password-connect-pushsecret-false-400-errors.md), which is
noise rather than breakage.

**After merge:** `kubectl get clustersecretstore onepassword-connect` `Ready`; `kubectl get
externalsecrets -A` with no `SecretSyncedError`; force one refresh (`just kube sync-es`) and
confirm a `lastRefreshTime` advances; the PushSecret in cert-manager still `Synced`.

**Rollback caveats:** revert the tag. The store CRD is stable across patch versions.

## CoreDNS

**Arrives as:** the `CoreDNS` group (chart plus the image mirrored via `mirror.gcr.io`).

**Repo paths:** `kubernetes/apps/kube-system/coredns/app/helm/values.yaml`. Deviations: the
`template ANY AAAA` plugin that answers every AAAA with an empty NOERROR (IPv4-only cluster), the
fixed `clusterIP`, `k8sAppLabelOverride: kube-dns`, and the PDB.

**Read before merging:** the CoreDNS release notes for plugin syntax changes; the Corefile is
rendered from `servers[].plugins`, and a renamed or removed plugin fails at pod start, not at
render.

**Known breaking patterns:** none recorded. Talos `forwardKubeDNSToHost` means the host DNS
cache sits in front of CoreDNS for pods, so a broken CoreDNS shows as stale answers before it
shows as NXDOMAIN.

**After merge:** both replicas Ready; `nslookup kubernetes.default` and an external name from a
fresh pod; an AAAA query returns NOERROR with no answer; no `plugin/errors` lines in the logs.

**Rollback caveats:** none beyond the tag.

## spegel

**Arrives as:** `spegel` (chart and image).

**Repo paths:** `kubernetes/apps/kube-system/spegel/app/helm/values.yaml`. Talos side:
`containerdRegistryConfigPath: /etc/cri/conf.d/hosts` matches the Talos registry-mirror
configuration in the machine config; the two must agree.

**Read before merging:** the spegel release notes for changes to the containerd hosts.toml layout
or the required containerd version.

**Known breaking patterns:** none as an upgrade. spegel's pod going 0/1 on one node is the
canonical symptom of that node's cross-node networking being broken
([KB-008](../troubleshooting/kb/008-cilium-cross-node-pod-networking-breaks.md)), so a spegel
bump landing alongside a network fault gets blamed first; check Cilium.

**After merge:** all three pods 1/1; pull a fresh image on one node and see a peer serve it in the
spegel logs; no `not found` bursts beyond the first minute.

**Rollback caveats:** none beyond the tag. Pulls fall back to the upstream registry while spegel
is down, slower but not broken.

## CloudNative-PG

**Arrives as:** `cloudnative-pg` (operator chart), the `postgresql` image (digest-pinned on the
`Cluster`), and the `CloudNative-PG Barman Cloud` group (the backup plugin).

**Repo paths:** `kubernetes/apps/database/cloudnative-pg/app/` (operator), `cluster/cluster.yaml`
(`postgres18`, 3 instances on `miroir-local`, `primaryUpdateStrategy: unsupervised`,
`failoverDelay: 60`), `cluster/scheduledbackup.yaml` and `cluster/objectstore.yaml` (Barman to
S3), `barman-cloud/` (the plugin), `backup/` (the NFS dump CronJob), and `cluster/pooler.yaml`.

**Read before merging:** the CNPG release notes for the operator minor (they call out required
`Cluster` spec changes and the supported Postgres versions), and the Barman plugin notes when the
group PR moves it. A **Postgres major** is not a Renovate concern: it is a planned migration with
a logical dump.

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| The operator bump rolls all three instances (a switchover then replica restarts) | Expected. Do it outside anything that cannot tolerate a few seconds of primary failover |
| The primary PDB allows 0 disruptions by design, so a node drain blocks until CNPG has switched over | Not a defect. For a planned reboot, patch `spec.enablePDB: false`; Flux restores it |
| A replica's data directory is node-local (`miroir-local`); a hard node reset can leave its `pg_control` mid-recovery, which only surfaces when the pod is next recycled (`PANIC: could not locate a valid checkpoint record`) | After any ungraceful reboot of a node hosting a replica, re-bootstrap it: delete the pod and its PVC, CNPG recreates via `pg_basebackup` |
| A plugin spec change on the `Cluster` rolls the affected pods and is exactly the recycle that exposes the row above | Check replica health before merging a barman-cloud bump |

**After merge:** `kubectl -n database get cluster postgres18` shows 3 ready instances and a
primary; the `Cluster` status lists streaming replicas with no lag (or `kubectl cnpg status` if
the plugin is installed); the next `ScheduledBackup` completes; one app that uses Postgres
(paperless, say) logs in.

**Rollback caveats:** the operator can be reverted one minor; the `Cluster` CRD is additive. A
Postgres image cannot go back a major and should not go back a minor once the data directory has
been opened by the newer one.

## gpu-operator

**Arrives as:** `nvidia` (the gpu-operator chart and its images) and `dcgm` (the exporter). The
driver and toolkit are **disabled** in the chart because Talos ships them as system extensions,
so the chart bump touches the device plugin, GFD, DCGM exporter and the operator itself.

**Repo paths:** `kubernetes/apps/gpu-operator/gpu-operator/app/helmrelease.yaml`
(`driver.enabled: false`, `toolkit.enabled: false`, `cdi.enabled: true`, time slicing via
`devicePlugin.config`), `app/time-slicing-config.yaml` (5 replicas per card),
`app/runtimeclass.yaml`, `app/prometheusrule.yaml`.

**Read before merging:** the gpu-operator release notes for the supported Kubernetes range (it is
the one operator here that publishes a hard ceiling; 26.7 documented 1.33 to 1.36) and the
required driver version against the Talos extension's driver.

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| Swapping the device-plugin implementation or rolling its DaemonSet races the kubelet's plugin registration; a node shows `allocatable.nvidia.com/gpu = 0` while the plugin pod logs claim success | Delete that node's plugin pod; allocatable returns in ~10 s. [KB-014](../troubleshooting/kb/014-gpu-device-plugin-handover-allocatable-zero.md) |
| The operator's Kubernetes ceiling is behind the running version | Watch its pods after any Kubernetes bump; nothing has broken yet at one minor ahead |

**After merge:** every node reports `nvidia.com/gpu: 5`; the DCGM exporter scrapes; a workload
with `runtimeClassName: nvidia` starts and sees the card (`nvidia-smi` in an Ollama pod).

**Rollback caveats:** none beyond the tag. The RuntimeClass is repo-owned, not chart-owned.

## Envoy Gateway

**Arrives as:** `envoyproxy` (the gateway-helm chart and the Envoy image). The Gateway API CRDs
arrive separately as a `gateway-api` `github-releases` bump, also protected (the guard matches
`/gateway-api/` since #3869), so they are reviewed by hand too.

**Repo paths:** `kubernetes/apps/network/envoy-gateway/app/helmrelease.yaml` (CRD policy
`CreateReplace`), `app/envoy.yaml` (the two `Gateway` objects and their `EnvoyProxy` configs; the
LB addresses in it are allowlisted functional config), `app/observability.yaml`. Every app's
route attaches to `envoy-internal` or `envoy-external` in `network`.

**Read before merging:** the Envoy Gateway release notes for the Gateway API version it targets
(the CRD bump must land first or alongside), and for `EnvoyProxy` / `ClientTrafficPolicy` /
`SecurityPolicy` schema changes.

**Known breaking patterns:** none recorded as an upgrade. The Cilium `socketLB.hostNamespaceOnly`
removal was made specifically so a `SecurityPolicy` targeting another route on the same gateway
works under DSR; none exist yet, so the first one is the first real test.

**After merge:** both Gateways `Programmed` with their addresses unchanged; a sample internal and
external route answer 200 through the gateway hostname (not the pod IP); external-dns has not
churned records (the OPNsense record count has a hard ceiling, see
[split DNS](../architecture/split-dns.md)); Gatus shows no new endpoint failures after the
sidecar's next refresh.

**Rollback caveats:** re-pin the chart tag. The Gateway API CRDs stay at the newer version, so
check the older Envoy Gateway release lists that CRD version as supported before reverting.

## tuppr

**Arrives as:** `tuppr` (chart and image). It drives the Talos and Kubernetes rolls above, so it
moves first whenever a Talos bump needs a tuppr feature (0.5.3 was required for the Image Factory
installer in 1.14).

**Repo paths:** `kubernetes/apps/system-upgrade/tuppr/app/helmrelease.yaml` (memory limit 768 Mi
after the apiserver-blip OOM), `upgrades/talosupgrade.yaml` (`rebootMode: default`, drain
settings, the two `healthChecks`), `upgrades/kubernetesupgrade.yaml`, `upgrades/prometheusrule.yaml`
(hand-tuned; the chart's rules are disabled).

**Read before merging:** the tuppr release notes; the CR schema (`TalosUpgrade`,
`KubernetesUpgrade`) has changed field names between minors.

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| Both pods die on an apiserver blip: leader `leader election lost` exit 1, follower OOMKilled on the reconnect re-list | Expected; the limit already has headroom. Not a leak |
| `rebootMode: powercycle` never fired on these Secure Boot nodes and left a node drained and cordoned on the old version | Keep `default`. Do not revert to `powercycle` |

**After merge:** `kubectl -n system-upgrade get talosupgrade,kubernetesupgrade` both `Completed`
or `Idle`; ServiceMonitor still scraping (`up{job=~"tuppr.*"}`); the dashboard renders.

**Rollback caveats:** none beyond the tag, provided no upgrade is in flight.

## VolSync and snapshot-controller

**Arrives as:** `volsync` (chart and mover images) and `snapshot-controller` (the CSI
VolumeSnapshot controller chart).

**Repo paths:** `kubernetes/apps/volsync-system/volsync/app/` (HR, the
`MutatingAdmissionPolicy` that adds the start jitter to mover Jobs, PrometheusRule), `volsync/maintenance/`
(Kopia maintenance and its policy), `kubernetes/apps/kube-system/snapshot-controller/app/`, and
the `kubernetes/components/volsync/` component every backed-up app pulls in.

**Read before merging:** the VolSync release notes for mover flag changes (the Kopia mover is
newer and moves faster than restic), and [Backups](backups.md).

**Known breaking patterns:**

| Pattern | Required action |
| --- | --- |
| The `MutatingAdmissionPolicy` API graduates or changes shape on a Kubernetes bump | Movers start without the jitter and all fire at once. Check one mover pod's spec has the `jitter` init container after the first post-bump run |
| The cache PVC was undersized for the shared Kopia index and every mover for one app failed with ENOSPC | [KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md) |
| `ReplicationDestination` objects were never updated alongside sources | [KB-031](../troubleshooting/kb/031-volsync-restore-destinations-never-updated.md) |

**After merge:** `just kube snapshot` and watch a handful of `ReplicationSource` objects reach
`Succeeded` on both the NFS and R2 legs; `kubectl get volumesnapshot -A` shows new snapshots
`READYTOUSE=true` and old ones pruned; the Kopia server pod has not restarted.

**Rollback caveats:** the Kopia repository format is forward-compatible within a major; check the
Kopia version inside the mover image before rolling back more than a patch.

## miroir

**Arrives as:** `miroir` (chart and image).

**Repo paths:** `kubernetes/apps/miroir-system/miroir/` (the CSI driver and its config) and the
`miroir-local` StorageClass it provides. CNPG replicas and other node-local volumes live on it.

**Read before merging:** the miroir release notes, with particular attention to anything about
loop-device lifecycle: the agent's failure to detach loop devices on unstage is what makes Talos
upgrades revert (see Talos above), so a release that fixes it changes that procedure.

**Known breaking patterns:** the loop leak. One node accumulates ~200 attached loop devices in a
few days and the agent re-attaches within 30 s of a manual detach. Not an upgrade breakage, but
every Talos node reboot has to budget for it until upstream fixes it.

**After merge:** every `miroir-local` PVC still bound and its pod Running; a new `miroir-local`
PVC provisions; loop-device count per node is not climbing faster than before.

**Rollback caveats:** the loopfile volumes are plain files under `/var/mnt/extra/miroir/volumes/`;
a chart revert does not touch them.
