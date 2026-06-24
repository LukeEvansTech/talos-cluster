# Troubleshooting

This section tracks known issues and their workarounds in the cluster, split into one
knowledge-base entry per issue. Each entry follows the same shape: **Symptom → Cause → Fix**.

## Symptom ladder

Work down from what you observe to the most likely entry:

- **Secrets / 1Password**
    - PushSecret logs spurious HTTP 400 errors but status shows `Synced` → [KB-001](kb/001-1password-connect-pushsecret-false-400-errors.md)
- **Flux / GitOps**
    - Flood of `FluxHelmReleaseArtifactFailed` ("flux is OOM"), or ~28 "dependency not ready" alerts while workloads stay healthy → [KB-007](kb/007-flux-not-ready-artifact-failed-alert-storms.md)
    - konflate checks fail on every open PR ("all CIs failing"), or `konflate-cache` runs out of inodes → [KB-011](kb/011-konflate-render-failures.md)
- **Networking**
    - One node's cross-node pod traffic flips/breaks while its host traffic is fine; its spegel pod goes `0/1` → [KB-008](kb/008-cilium-cross-node-pod-networking-breaks.md)
- **Storage / backups**
    - Backup pod stuck `PodInitializing` (`mount.nfs: Failed to resolve`), or `CreateContainerConfigError` on a subPath → [KB-009](kb/009-nfs-mount-failures-host-dns-readonly-export.md)
    - After a Rook v1.20 upgrade: RBD nodeplugin `FailedCreate`, or ~88 `VolSyncVolumeOutOfSync` alerts → [KB-010](kb/010-rook-ceph-v120-csi-driver-split.md)
    - `volsync-system/kopia` repo server OOM-crashloops (`exit 137`) → [KB-016](kb/016-kopia-repo-server-oom-repo-size.md)
- **Workloads / pods**
    - A JVM/Logstash pod OOMKills on a cadence despite a bounded heap → [KB-012](kb/012-jvm-container-rss-oom-malloc-arena-max.md)
    - A pure-Go pod SIGSEGVs (`exit 139`) on a large fraction of starts, before any logs → [KB-013](kb/013-go-1264-binary-startup-sigsegv.md)
    - HelmRelease `UpgradeFailed`/rollback loop, pod stuck `ContainerCreating` with a `Pulling` event (large image) → [KB-015](kb/015-slow-image-pulls-exceed-helmrelease-timeout.md)
    - `allocatable.nvidia.com/gpu = 0` for minutes after a device-plugin swap → [KB-014](kb/014-gpu-device-plugin-handover-allocatable-zero.md)
- **Plex playback**
    - 4K direct-play freezes for ~60s every ~6 minutes on LAN Apple TVs → [KB-002](kb/002-plex-direct-play-buffering-bbr-mtu-probing.md)
    - "Server unavailable" / connection drops at session start, pod otherwise healthy → [KB-003](kb/003-plex-advertises-broken-connection-urls.md)
    - Remote 4K titles crash with `bad lexical cast`; the same titles work on phone/LAN → [KB-018](kb/018-plex-remote-4k-transcode-decision-crash.md)
- **Talos upgrades**
    - TUPPR patch rollout stuck after drain; node cordoned and still on the old version → [KB-004](kb/004-talos-patch-rollout-gotchas-tuppr.md)
- **CI / validation / local dev**
    - `Flate - Test` fails or skips on the `gpu-operator` namespace → [KB-005](kb/005-flate-misresolves-ngc-helmrepository-chart-urls.md)
    - Checkov CKV_K8S_21 flags a namespaced resource as `default` → [KB-006](kb/006-checkov-ckv-k8s-21-namespaced-resources.md)
    - First commit after a mise tool bump dies with `ln -sf ... File exists` → [KB-017](kb/017-mise-lefthook-symlink-race-on-commit.md)

## All entries

- [KB-001: 1Password Connect PushSecret False 400 Errors](kb/001-1password-connect-pushsecret-false-400-errors.md)
- [KB-002: Plex Direct-Play Buffering on LAN Apple TVs (BBR + MTU Probing)](kb/002-plex-direct-play-buffering-bbr-mtu-probing.md)
- [KB-003: Plex Advertises Broken Connection URLs To plex.tv](kb/003-plex-advertises-broken-connection-urls.md)
- [KB-004: Talos Patch Rollout Gotchas (TUPPR)](kb/004-talos-patch-rollout-gotchas-tuppr.md)
- [KB-005: flate Mis-Resolves NGC HelmRepository Chart URLs](kb/005-flate-misresolves-ngc-helmrepository-chart-urls.md)
- [KB-006: Checkov CKV_K8S_21 Flags Namespaced Resources Without an Explicit Namespace](kb/006-checkov-ckv-k8s-21-namespaced-resources.md)
- [KB-007: Flux "not ready" / "artifact failed" Alert Storms](kb/007-flux-not-ready-artifact-failed-alert-storms.md)
- [KB-008: Cross-Node Pod Networking Breaks (Cilium)](kb/008-cilium-cross-node-pod-networking-breaks.md)
- [KB-009: NFS Mount Failures (Host DNS / Read-Only Export)](kb/009-nfs-mount-failures-host-dns-readonly-export.md)
- [KB-010: Rook-Ceph v1.20 CSI Driver Split Gotchas](kb/010-rook-ceph-v120-csi-driver-split.md)
- [KB-011: konflate Render Failures (Cache Inode Fill / Phantom Mirror)](kb/011-konflate-render-failures.md)
- [KB-012: JVM / Logstash Container RSS OOM Despite a Bounded Heap (`MALLOC_ARENA_MAX`)](kb/012-jvm-container-rss-oom-malloc-arena-max.md)
- [KB-013: Go 1.26.4 Binary SIGSEGV at Startup (Before Any Logging)](kb/013-go-1264-binary-startup-sigsegv.md)
- [KB-014: GPU Device-Plugin Handover Leaves `allocatable.nvidia.com/gpu = 0`](kb/014-gpu-device-plugin-handover-allocatable-zero.md)
- [KB-015: Slow Image Pulls Exceed the HelmRelease Timeout (Rollback Loop)](kb/015-slow-image-pulls-exceed-helmrelease-timeout.md)
- [KB-016: Kopia Repo Server OOM = Repo Size, Not a Maintenance Failure](kb/016-kopia-repo-server-oom-repo-size.md)
- [KB-017: `mise` + lefthook Symlink Race Blocks the First Commit After a Tool Bump](kb/017-mise-lefthook-symlink-race-on-commit.md)
- [KB-018: Plex Remote 4K Transcode-Decision Crash (`bad lexical cast`)](kb/018-plex-remote-4k-transcode-decision-crash.md)
