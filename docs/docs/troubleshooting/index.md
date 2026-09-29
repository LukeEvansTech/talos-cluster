# Troubleshooting

This section tracks known issues and their workarounds in the cluster, split into one
knowledge-base entry per issue. Each entry follows the same shape: symptom, cause, fix.

Before fixing anything, check [known noise and non-remediation](known-noise.md): the alerts and
symptoms where the obvious corrective action is wrong, the configuration that is deliberately
"broken", and the operations that must be escalated rather than attempted. After a change to
protected infrastructure, the [health verdict](../operations/health-verdict.md) is the
cross-cutting check.

## Symptom ladder

Work down from what you observe to the most likely entry:

- **Secrets / 1Password**
  - PushSecret logs spurious HTTP 400 errors but status shows `Synced` → [KB-001](kb/001-1password-connect-pushsecret-false-400-errors.md)
  - GitGuardian flags plaintext credentials or certificate keys in the network-config backup repository → [KB-070](kb/070-firewall-backup-leaked-secrets.md)
- **Flux / GitOps**
  - Flood of `FluxHelmReleaseArtifactFailed` ("flux is OOM"), or ~28 "dependency not ready" alerts while workloads stay healthy → [KB-007](kb/007-flux-not-ready-artifact-failed-alert-storms.md)
  - konflate checks fail on every open PR ("all CIs failing"), or `konflate-cache` runs out of inodes → [KB-011](kb/011-konflate-render-failures.md)
- **Networking**
  - One node's cross-node pod traffic flips/breaks while its host traffic is fine; its spegel pod goes `0/1` → [KB-008](kb/008-cilium-cross-node-pod-networking-breaks.md)
  - An app 404s through the gateway on its real hostname but works on its pod IP (live HTTPRoute drifted to `*.example.com`) → [KB-020](kb/020-httproute-drifts-to-placeholder-hostnames.md)
  - `NodeHighNumberConntrackEntriesUsed` on every node at once right after deploying a scanner → [KB-023](kb/023-node-conntrack-saturation-host-network-scanner.md)
  - A dozen Gatus endpoints across `media`/`downloads` go red at once with HTTP 503, DNS still resolves, pods are simply absent (zeroscaler at 0/1) → [KB-027](kb/027-dns-cleanup-scaled-nfs-apps-to-zero.md)
  - One internal hostname never gets its record (or resolves to the wrong value) while every other new app is fine; the name is already in Unbound as a hand-made row → [KB-033](kb/033-opnsense-record-exists-but-is-unowned.md)
  - `OPNsenseDeleteBlocked` fires, or opnsense-dns fails every reconcile with `row has alias children; refusing to delete` → [KB-034](kb/034-opnsense-delete-blocked-by-alias.md)
  - A certificate on a Traefik instance outside the cluster nears expiry and nothing alerts; ACME renewal has been failing quietly → [KB-049](kb/049-acme-token-ip-allowlist-broke-renewal-silently.md)
- **Storage / backups**
  - Backup pod stuck `PodInitializing` (`mount.nfs: Failed to resolve`), or `CreateContainerConfigError` on a subPath → [KB-009](kb/009-nfs-mount-failures-host-dns-readonly-export.md)
  - After a Rook v1.20 upgrade: RBD nodeplugin `FailedCreate`, or ~88 `VolSyncVolumeOutOfSync` alerts → [KB-010](kb/010-rook-ceph-v120-csi-driver-split.md)
  - `volsync-system/kopia` repository server OOM-crashloops (`exit 137`) → [KB-016](kb/016-kopia-repo-server-oom-repo-size.md)
  - `CephMonDownQuorumAtRisk` (critical) fires minutes after cordoning a control-plane node → [KB-019](kb/019-cordon-control-plane-breaks-ceph-mon-quorum.md)
  - Snapshot CRDs vanish minutes after merging a chart migration; controller crash-loops on "failure to ensure CRDs exist"; HelmRelease rollback loop re-deletes them each retry → [KB-029](kb/029-chart-migration-deletes-keep-annotated-crds.md)
  - One app's `volsync-src-<app>-nfs-*` mover pods sit in `Error` while every other app backs up fine; the log ends `write /cache/CACHEDIR.TAG: no space left on device` then `found existing data in storage location` → [KB-030](kb/030-volsync-kopia-cache-pvc-too-small.md)
  - A restore hangs on an unbindable cache PVC, is offered less capacity than the data, or hits ENOSPC on `/cache`, while every backup is green → [KB-031](kb/031-volsync-restore-destinations-never-updated.md)
  - The Docker-estate backup reports success every night but the copied data never changes → [KB-064](kb/064-docker-backup-frozen-nas-snapshot-silent-success.md)
  - The Veeam server is down for days and nothing pages; backup jobs simply stop advancing → [KB-067](kb/067-veeam-server-bsod-silent-outage.md)
  - An app's VolSync backups are green but a restore would miss its real data (satisfactory backs up the wrong PVC) → [KB-145](kb/145-satisfactory-volsync-wrong-pvc.md)
- **Workloads / pods**
  - A JVM/Logstash pod OOMKills on a cadence despite a bounded heap → [KB-012](kb/012-jvm-container-rss-oom-malloc-arena-max.md)
  - A pure-Go pod SIGSEGVs (`exit 139`) on a large fraction of starts, before any logs → [KB-013](kb/013-go-1264-binary-startup-sigsegv.md)
  - HelmRelease `UpgradeFailed`/rollback loop, pod stuck `ContainerCreating` with a `Pulling` event (large image) → [KB-015](kb/015-slow-image-pulls-exceed-helmrelease-timeout.md)
  - `allocatable.nvidia.com/gpu = 0` for minutes after a device-plugin swap → [KB-014](kb/014-gpu-device-plugin-handover-allocatable-zero.md)
  - `CreateContainerConfigError: runAsUser breaks non-root policy` on a fresh render of an s6/LinuxServer image → [KB-022](kb/022-s6-image-createcontainerconfigerror-non-root.md)
  - One `KubeJobFailed` a day for `netbox-housekeeping` (`Unknown command: 'housekeeping'`), or NetBox housekeeping silently stopped with a `scheduled` job stuck in the past → [KB-032](kb/032-netbox-housekeeping-removed-command-and-wedged-system-job.md)
  - A gluetun VPN sidecar restarts continuously and never reaches `Ready`, often `OOMKilled` on every cycle → [KB-042](kb/042-gluetun-kubelet-probe-restart-loop.md)
  - SABnzbd's post-processing queue stops advancing while downloads keep completing → [KB-059](kb/059-sabnzbd-post-processing-helper-hang.md)
  - A once-a-day job goes silent for days while `/health` answers OK and the pod restarts once a day → [KB-085](kb/085-lurcher-oom-invisible-to-health-check.md)
  - An `*arr` app returns HTTP 401 on the correct password and logs an encryption error → [KB-113](kb/113-arr-readonly-root-no-tmp-401-on-correct-password.md)
  - A custom image crashloops right after a Renovate digest bump that changed its base runtime → [KB-157](kb/157-digest-bump-jruby.md)
  - Prowler's worker and beat pods restart in a loop on a Dragonfly authentication error → [KB-159](kb/159-prowler-dragonfly-auth-silent-failure.md)
- **Monitoring / Grafana**
  - Every panel on one dashboard shows "No data" / "Datasource Prometheus was not found" → [KB-021](kb/021-grafana-dashboard-panels-blank-datasource-case.md)
  - `BmcEventLogWarning` fires dozens of times for one hardware fault → [KB-037](kb/037-bmc-event-log-per-entry-alert-storm.md)
  - Storage-host node and SMART series appear twice, and unscoped queries or mixin rules double-count → [KB-038](kb/038-truenas-double-scraped-on-both-ports.md)
  - A Gatus endpoint goes down and nothing pages → [KB-065](kb/065-gatus-allowlist-dropped-two-groups-silently.md)
- **Mail relay (smtp2graph)**
  - `SMTP2GraphQueueStalled` holds at one queued message for hours while the canary and later mail deliver fine; the log shows a single `Failed to send message` and no retries → [KB-035](kb/035-smtp2graph-message-stranded-in-queue.md)
- **Plex playback**
  - 4K direct-play freezes for ~60s every ~6 minutes on LAN Apple TVs → [KB-002](kb/002-plex-direct-play-buffering-bbr-mtu-probing.md)
  - "Server unavailable" / connection drops at session start, pod otherwise healthy → [KB-003](kb/003-plex-advertises-broken-connection-urls.md)
  - Remote 4K titles crash with `bad lexical cast`; the same titles work on phone/LAN → [KB-018](kb/018-plex-remote-4k-transcode-decision-crash.md)
  - Apple TV shows one frame of a 4K title then the Plex app freezes (force-quit to recover); the same file plays fine in Infuse → [KB-026](kb/026-plex-apple-tv-app-receive-window-deadlock.md)
- **Talos upgrades**
  - TUPPR patch rollout stuck after drain; node cordoned and still on the old version → [KB-004](kb/004-talos-patch-rollout-gotchas-tuppr.md)
  - Upgrade reports success but the node comes back Ready, uncordoned, and still on the old version; it rebooted promptly → [KB-028](kb/028-talos-upgrade-boots-old-version-loaderentrydefault.md) (NVRAM wipe left a stale `LoaderEntryDefault`)
  - Same, but the node took about 5 minutes to go down after the install completed → the reboot-sequence teardown revert in [Talos upgrades](../operations/talos-upgrades.md#upgrade-didnt-take-node-reboots-into-the-old-version)
  - A node cannot unseal its disk after a BIOS update wipes the Secure Boot keys → [KB-207](kb/207-tpm-luks-slot-survives-bios-secure-boot-wipe.md)
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
- [KB-013: Go Pod Startup SIGSEGV Was a UPX Stub vs. Service-Link Env Vars, Not a Go Regression](kb/013-go-1264-binary-startup-sigsegv.md)
- [KB-014: GPU Device-Plugin Handover Leaves `allocatable.nvidia.com/gpu = 0`](kb/014-gpu-device-plugin-handover-allocatable-zero.md)
- [KB-015: Slow Image Pulls Exceed the HelmRelease Timeout (Rollback Loop)](kb/015-slow-image-pulls-exceed-helmrelease-timeout.md)
- [KB-016: Kopia Repository Server OOM = Repository Size, Not a Maintenance Failure](kb/016-kopia-repo-server-oom-repo-size.md)
- [KB-017: `mise` + lefthook Symlink Race Blocks the First Commit After a Tool Bump](kb/017-mise-lefthook-symlink-race-on-commit.md)
- [KB-018: Plex Remote 4K Transcode-Decision Crash (`bad lexical cast`)](kb/018-plex-remote-4k-transcode-decision-crash.md)
- [KB-019: Cordoning a Control-Plane Node Breaks Ceph Mon Quorum](kb/019-cordon-control-plane-breaks-ceph-mon-quorum.md)
- [KB-020: App Returns 404 Through the Gateway (HTTPRoute Drifted to Placeholder Hostnames)](kb/020-httproute-drifts-to-placeholder-hostnames.md)
- [KB-021: Grafana Dashboard Panels All Blank ("Datasource … was not found")](kb/021-grafana-dashboard-panels-blank-datasource-case.md)
- [KB-022: Container Won't Start as Non-Root (s6 / LinuxServer Image `CreateContainerConfigError`)](kb/022-s6-image-createcontainerconfigerror-non-root.md)
- [KB-023: Node Conntrack Table Saturates from a Host-Network Scanner](kb/023-node-conntrack-saturation-host-network-scanner.md)
- [KB-024: zeroscaler, NFS-Availability Scale-to-Zero via Native HPA](kb/024-zeroscaler-nfs-hpa.md)
- [KB-025: CephFS "Module ceph not found" on Talos Is a Built-in, Not a Missing Module](kb/025-cephfs-modprobe-builtin-misdiagnosis.md)
- [KB-026: Plex Apple TV App Freezes on One Frame (Client Receive-Window Deadlock)](kb/026-plex-apple-tv-app-receive-window-deadlock.md)
- [KB-027: A DNS Cleanup Scaled Every NFS-Backed App to Zero](kb/027-dns-cleanup-scaled-nfs-apps-to-zero.md)
- [KB-028: Talos Upgrade Installs Successfully but the Node Boots the Old Version (NVRAM Wipe)](kb/028-talos-upgrade-boots-old-version-loaderentrydefault.md)
- [KB-029: Chart Migration Deletes Keep-Annotated CRDs (postRenderer Removal Races the Chart Swap)](kb/029-chart-migration-deletes-keep-annotated-crds.md)
- [KB-030: VolSync Kopia Backups Fail with `no space left on device` on `/cache`](kb/030-volsync-kopia-cache-pvc-too-small.md)
- [KB-031: VolSync Restore Destinations Silently Frozen at Creation-Time Values](kb/031-volsync-restore-destinations-never-updated.md)
- [KB-032: NetBox Housekeeping Fails Two Ways at Once (Removed Command, Wedged System Job)](kb/032-netbox-housekeeping-removed-command-and-wedged-system-job.md)
- [KB-033: OPNsense Record Exists but Is Unowned (No Registry TXT Row)](kb/033-opnsense-record-exists-but-is-unowned.md)
- [KB-034: OPNsense Delete Blocked by a Hand-Made Alias](kb/034-opnsense-delete-blocked-by-alias.md)
- [KB-035: smtp2graph Message Stranded in the Queue (Never Retried, Never Failed)](kb/035-smtp2graph-message-stranded-in-queue.md)
- [KB-037: BmcEventLogWarning pages once per SEL entry instead of once per fault](kb/037-bmc-event-log-per-entry-alert-storm.md)
- [KB-038: TrueNAS host metrics scraped twice under two job names](kb/038-truenas-double-scraped-on-both-ports.md)
- [KB-042: gluetun Kubelet Probes Cause a Self-Sustaining Restart Loop](kb/042-gluetun-kubelet-probe-restart-loop.md)
- [KB-049: ACME renewal broke silently on two non-cluster Traefik instances](kb/049-acme-token-ip-allowlist-broke-renewal-silently.md)
- [KB-059: A hung par2/unrar/7z helper stalls the entire SABnzbd queue](kb/059-sabnzbd-post-processing-helper-hang.md)
- [KB-064: Docker-estate backup silently copied a frozen NAS snapshot for four nights](kb/064-docker-backup-frozen-nas-snapshot-silent-success.md)
- [KB-065: Gatus alerting was off for months, then an allowlist dropped two more groups](kb/065-gatus-allowlist-dropped-two-groups-silently.md)
- [KB-067: Veeam B&R server BSOD sat unnoticed for 12 days](kb/067-veeam-server-bsod-silent-outage.md)
- [KB-070: Oxidized's firewall backup leaked plaintext secrets into Git](kb/070-firewall-backup-leaked-secrets.md)
- [KB-085: lurcher ran out of memory every day for 12 days, invisible to its own health check](kb/085-lurcher-oom-invisible-to-health-check.md)
- [KB-113: `*arr` app returns 401 on the correct password (no writable /tmp)](kb/113-arr-readonly-root-no-tmp-401-on-correct-password.md)
- [KB-145: satisfactory's VolSync backup may still target the wrong PVC](kb/145-satisfactory-volsync-wrong-pvc.md)
- [KB-157: Renovate digest bump silently changed a custom image's base runtime and broke its plugin](kb/157-digest-bump-jruby.md)
- [KB-159: Prowler's Celery Worker Never Authenticated to Dragonfly](kb/159-prowler-dragonfly-auth-silent-failure.md)
- [KB-207: A Static-Passphrase LUKS Slot Insures Against a BIOS Secure Boot Key Wipe](kb/207-tpm-luks-slot-survives-bios-secure-boot-wipe.md)
