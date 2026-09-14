# Known noise and non-remediation

**Read this before "fixing" an alert.** Every entry below is a case where the obvious corrective
action is wrong, and several are cases where it makes things worse. The audience is anyone acting
on an alert without the history of how it was last diagnosed: a fresh session, another machine,
an agent behind the read-only MCP servers, or a human at 3 a.m.

The [symptom ladder](index.md) answers "what is this and how do I fix it". This page answers the
question that comes first: **should I be touching this at all?**

## How to use this page

1. **A match means "do not remediate". It does not mean "ignore".** Record which entry matched and
   why, in the PR, the issue or the session notes, so a wrong match is auditable afterwards. An
   alert closed with no note is indistinguishable from an outage nobody handled.
2. **Match on the signature, not the alert name.** Most entries share an alert name with a genuine
   fault. The signature column is what separates them. If the signature does not fit, treat the
   alert as real.
3. **If the cluster contradicts the entry, believe the cluster.** Every entry carries the check that
   re-verifies it. An entry that fails its own check is a bug in this page: fix the page, do not
   work around it.
4. **When in doubt, escalate.** A needless escalation costs a human two minutes. A wrongly
   suppressed incident costs far more.

The test for adding an entry: **if this entry were wrong, what would it cause someone to ignore?**
If the answer is "a real outage", it does not belong here. Put it in the symptom ladder instead,
with the diagnosis that tells the two apart.

## Do not act: benign signatures

### `KubeJobFailed` for a CronJob whose last run succeeded

**Signature:** `KubeJobFailed` firing for hours or days; the owning CronJob's
`status.lastSuccessfulTime` is recent; the failed Job's pod is long gone.

**Why:** the rule is `kube_job_failed > 0`, which is true for as long as the failed **Job object**
exists. One transient failure pins the alert until history rotation or TTL removes the object.
Three unrelated `KubeJobFailed` alerts sharing an identical `startsAt` is a second tell: that
timestamp is the last Alertmanager restart, not the failure.

**Re-verify:**

```bash
kubectl -n <ns> get cronjob <name> -o jsonpath='{.status.lastSuccessfulTime}'
```

**Do instead:** delete the stale Job (`kubectl -n <ns> delete job <name>`). Silencing or
reconciling does nothing. House style is `ttlSecondsAfterFinished: 86400` on every CronJob so
this self-clears; check the CronJob has it. See
[KB-032](kb/032-netbox-housekeeping-removed-command-and-wedged-system-job.md) for the latching
behaviour in context.

### `BmcEventLogWarning` and other "an event-log entry exists" alerts

**Signature:** an alert whose expression is the presence of a BMC System Event Log entry
(`idrac_events_log_entry`), for a fault dated days ago, while the live health metric for that
component (`idrac_power_supply_health`, and so on) reads OK.

**Why:** the SEL is history, not state. A power-supply failure logged during a power cut stays in
the log for the exporter's `maxage` window (7 days) after the supply is back to normal.

**Re-verify:** read the health metric for the component named in the entry before believing the
log entry. Clearing the SEL is cosmetic while the hardware is still faulting, and it is
irreversible: dump it first.

### Short `CephMonClockSkew` / `CephHealthWarning` episodes

**Signature:** either alert fires and resolves within 5 to 15 minutes, landing on 5-minute
boundaries, with no node reboot and no NTP change.

**Why:** ordinary clock wander against WAN NTP peaks around 111 ms over a month on this cluster,
and Ceph's `mon_clock_drift_allowed` default was 50 ms. The mon only samples skew every 300 s
(`mon_timecheck_interval`), so one unlucky sample pages and the next clears it. The threshold was
raised to 0.3 s on the cluster side (#4819) and `CephHealthWarning` is inhibited whenever a
specific `Ceph*` alert already describes the condition.

**Re-verify:** `ceph config get mon mon_clock_drift_allowed` in the toolbox should read `0.3`. A
skew of **seconds** is a genuine fault and still trips it.

### Flux "dependency not ready" floods

**Signature:** 20 to 30 Kustomizations go not-ready at once while every workload stays healthy.

**Why:** one Flux source failed (a registry 504 on the only two quay-hosted charts, cert-manager
and Cilium) and everything downstream of it in the `dependsOn` graph re-alerts on every retry.
Bulk-deleting a widely referenced Kustomize component directory produces the same shape for one
reconcile.

**Re-verify:** `flux get kustomizations -A --status-selector ready=false` and walk the
`dependsOn` chain to the root. Check the root's OCIRepository, not its HelmRelease, which stays
Ready on the cached chart.

**Do instead:** reconcile the root source; the cascade clears itself. Full diagnosis in
[KB-007](kb/007-flux-not-ready-artifact-failed-alert-storms.md).

### Transient image-pull errors right after a merge

**Signature:** `ErrImagePull` / `connection reset by peer` / a spegel `not found` on a pod that is
seconds old.

**Why:** the mirror and the registry are still catching up. These self-heal on backoff in 60 to
90 seconds. Persistent pull failures on **one** node point at that node's egress, not at the
upgrade. Large images can also exceed the HelmRelease timeout, which is a different fault:
[KB-015](kb/015-slow-image-pulls-exceed-helmrelease-timeout.md).

### Operator-managed restarts after a protected-infra bump

**Signature:** within about five minutes of a Cilium, CNPG or Rook change, a burst of restarts and
short Gatus alerts.

**Why:** Cilium's agent DaemonSet rolls on any config change and briefly dips apiserver
connectivity. Observed consequences that clear themselves: netbox readiness flaps (1 s probe
timeout), both tuppr pods die (leader `leader election lost` exit 1, follower OOMKilled on the
reconnect re-list), notification-controller restarts. CNPG rolls its Postgres instances on an
operator bump, which is expected.

**Not noise in the same burst:** `coder` does not self-heal after the Cilium roll (in-memory
listener closed, pod stays 0/1). Restart it explicitly.

### `leader election lost` and a follower OOMKill on an apiserver blip

**Signature:** two replicas of a controller-runtime operator die at the same second on different
nodes. The leader exits 1 with `leader election lost`; the follower is OOMKilled once.
`restartCount` is 1 and steady-state memory is flat.

**Why:** losing the lease and exiting is HA by design. The follower's death is the simultaneous
re-LIST of every informer on reconnect, a spike on top of steady state, not a leak. A leak climbs
over time and OOMs on a schedule.

**Do instead:** give the operator memory headroom (tuppr went 512 Mi to 768 Mi, #3756). Do not
chase a leak.

### Restarts that cluster inside an upgrade window

**Signature:** an operator shows N restarts, all of them timestamped inside a Talos or Kubernetes
roll, and none since.

**Why:** reconcile churn. `ai/litellm-operator` showed 8 OOMKills, all inside the 2026-09-03/04
upgrade window and zero in the three days after. "Churn-correlated" and "pre-existing" are
different claims; say which the timestamps support before raising a limit.

### dispatcharr boot-time `ERROR` lines

**Signature:** on every dispatcharr start, `Redis configuration error: … CONFIG SET - 'save'` and
one `FATAL: database "dispatch" does not exist`.

**Why:** both are upstream bugs. The app runs `CONFIG SET save ''` on every new client and
Dragonfly rejects it (a shared Dragonfly should); `pg_isready` is run as the app user without
`-d`. Its Celery worker is alive and EPG refreshes run hourly.

### A Netdata child reporting `alarms: {}`

**Signature:** `/api/v1/alarms` on a streaming child (`netdata-k8s-state`, the per-node pods)
returns `{"status": false, "alarms": {}}` and someone reads that as "no alarms raised".

**Why:** health runs on `netdata-parent` only. The child's empty answer is "health disabled here",
not "healthy". Query the parent's mirrored host: `/host/<child>/api/v1/alarms?all`. Netdata alarms
never appear in Alertmanager either; check both paths when chasing a page.

## Do not "fix": deliberate configuration that looks broken

### The CNPG primary PDB at `ALLOWED DISRUPTIONS: 0`

Blocks node drains by design: CNPG forces a switchover rather than evicting the primary. During a
planned rolling reboot, patch `spec.enablePDB: false` on the `Cluster`; Flux reconciles it back
on its own. Do not delete the PDB. Procedure in [Talos upgrades](../operations/talos-upgrades.md).

### The muted Ceph `AUTH_INSECURE_*` health checks

Several `AUTH_INSECURE_*` checks are muted after the CVE-2025-30156 key rotation
(`keyGeneration: 2`). The mutes, and the comment explaining why each one is safe, live in the Rook
cluster HelmRelease under `cephClusterSpec.healthCheck`. A `CephHealthWarning` whose only detail
is one of those is the mute, not a regression.

### The `cluster-secrets` placeholder Secret has no `ssa` annotation on the live object

The fix for the placeholder race (#4081) is `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent` on the
**git manifest**. External Secrets rewrites the live Secret's metadata a second after Flux applies
it and drops the annotation. That is expected. Verify by the managedFields timestamp of
`kustomize-controller` on the Secret, which must stop advancing, not by reading the annotation.
[KB-020](kb/020-httproute-drifts-to-placeholder-hostnames.md).

### Konflate `failures` on a PR

The Konflate status is advisory. `failures` means individual resources could not render (the
shelly-fleet private source cascade is on every PR); `error` means the whole render failed and is
the only state that blocks. Do not chase `failures` unless a resource you changed is among them.

### A Renovate PR held red on purpose

`romm` is held at 5.0.0 (5.1.0 runs an un-disableable nightly `rmtree` over the artwork PVC,
rommapp/romm#3994). `garage` v2.4.0 is blocked by `allowedVersions` (rustls no-CryptoProvider
panic). A red `claude/renovate-review` status on those PRs is the hold. Do not re-run it, admin
merge it, or close and reopen the PR (Renovate then blocks the update).

### Both `FilesystemTrimConfig` and the `kube-system/fstrim` CronJob

Talos native trim only walks volumes Talos mounts itself. The CronJob is what trims Ceph RBD PVC
filesystems and miroir loop devices. Keep both.

### Gatus endpoints for apps scaled to zero

zeroscaler scales NFS-dependent apps to 0 replicas when the NAS is unavailable, and Gatus then
reports them down. That is the design working. Opt an intentionally-idle app out with
`gatus.home-operations.com/enabled: "false"` on its route rather than "fixing" the endpoint.
[KB-024](kb/024-zeroscaler-nfs-hpa.md), [KB-027](kb/027-dns-cleanup-scaled-nfs-apps-to-zero.md).

## Read the evidence, not the alert: signatures that name the wrong fault

Each of these presents as one thing and is another. The check in each row is what tells them
apart; run it before acting on the presenting symptom.

| Presents as | Actually | The check |
| --- | --- | --- |
| SNMP target `up == 0`, device "flapping" 100+ times a day | One slow MIB module on a multi-module target; `up` reflects the slowest module | `kubectl -n observability logs deploy/snmp-exporter \| grep <target>` names the module that timed out |
| `RedfishNodeDown`, BMC "unreachable" several times a day | Per-request timeout tripping on a cold first request | `redfish_scrape_duration_seconds` pinned at exactly the timeout (15.02 s) |
| gluetun `Init:CrashLoopBackOff` with VPN-looking logs (endpoints rotating, DNS timeouts) | OOMKilled 2 s into a warm start (`servers.json` merge + blocklist build) | `lastState.terminated.reason: OOMKilled`; last log line is `DNS server listening`, never `[dns] ready` |
| An `*arr` login returns 401 on the **correct** password; a wrong one gets the normal redirect | `readOnlyRootFilesystem` with no `tmp` emptyDir; ASP.NET cannot stage a data-protection key | `grep -A4 'reading the key ring'` in the app log shows `Read-only file system : '/tmp/'` |
| No `KubePodCrashLooping`, only a vague `KubePodNotReady` | A liveness-probe kill of an app that exits 0 on SIGTERM; `CrashLoopBackOff` never triggers | `kubectl get pod -o jsonpath='{.status.containerStatuses[].restartCount}'`; litellm hit 62 restarts this way |
| `ShellyDeviceOutOfSync` claiming config drift | Device unreachable or rate-limited (HTTP 429); `Unknown` is mapped to `False` | `driftedSections` is empty on the `ShellyDevice` status; message names the 429 |
| shelly-exporter pod is always only minutes old | Devices are flapping (a Wi-Fi AP dropped); each membership flip rewrites its ConfigMap and Reloader bounces it | `sort_desc(changes(shelly_device_online[12h]))` ranks the flappers |
| Plex (or any NFS app) at 0/1 with NFS healthy | blackbox's NFS probe fails after the exporter pod reschedules (stale resolver state) | NFS resolves and connects from a `netshoot` pod in the same namespace |
| `GpuHighMemoryUsage` blaming a transcoder pod | DCGM stamps a time-sliced card's whole `FB_USED` on one co-tenant at a time | Compare the series across an hour: the label alternates between tenants |
| A Gatus internal endpoint fails DNS on a name that should exist | The live HTTPRoute drifted to the placeholder hostname | `kubectl get httproute -A -o json \| jq '.items[] \| select(.spec.hostnames[]? \| test("example"))'` |
| Ceph `HEALTH_WARN: N daemons have recently crashed` blocking tuppr | Archived-or-not crash reports from a daemon that recovered on its own | `ceph crash ls-new`; archive them, then re-check `ceph health` |

## Escalate: do not attempt these autonomously

- **Never run `kopia` CLI commands inside the `volsync-system/kopia` server pod.** `snapshot list
  --all` loads the repo index into the serving container and OOMKills it, taking the shared
  backup server down for every app. Use a separate short-lived pod against the same NFS mount.
- **Never `kubectl patch` the size of a GitOps-managed PVC.** A PVC cannot shrink, so the next
  Helm upgrade fails `field can not be less than status.capacity` and the HelmRelease wedges. Bump
  `size:` in git ([KB-011](kb/011-konflate-render-failures.md) has the history).
- **Never reboot or upgrade all three nodes at once.** With Rook-Ceph, the last node stalls forever
  in the volume-unmount step of shutdown because the other two mons are already gone. Drain one,
  reboot one, wait for `HEALTH_OK` and three healthy etcd members, then the next.
  [Talos upgrades](../operations/talos-upgrades.md) has the procedure and its guards.
- **Never force-delete `Terminating` pods on a node that is unreachable but not confirmed dead.**
  If its containers are still running, a replacement pod gives an RWO volume two writers.
- **A Ceph major is one way.** Reverting the Rook cluster chart does not downgrade the Ceph
  daemons. Rolling back across a Ceph major is a rebuild decision, not a `git revert`.
- **Anything a human explicitly deferred** (a held version, a declined backup, an accepted risk
  recorded in the [hardening backlog](../operations/hardening-backlog.md)) stays deferred until
  they say otherwise.

## Not noise: remediated with a known fix

These fire for a real reason and have a fixed, safe remedy. Apply it, then verify.

| Alert or symptom | Remedy | Verify |
| --- | --- | --- |
| Ceph `HEALTH_WARN` from recent crash reports (blocks tuppr) | `ceph crash archive-all` in the toolbox, after `ceph crash ls-new` shows the daemon recovered | `ceph health` returns `HEALTH_OK` |
| `volsync-system/kopia` server OOM-crashlooping | Raise its memory limit; it scales with repo size | [KB-016](kb/016-kopia-repo-server-oom-repo-size.md) |
| `KubeJobFailed` with a healthy CronJob | `kubectl delete job <name>` | Alert resolves within one evaluation |
| CSI plugin pods still on the old cephcsi image after a Rook bump | `kubectl -n rook-ceph rollout restart deploy/ceph-csi-controller-manager` | Every `*.csi.ceph.com-*plugin` pod runs the image-set version |
| VolumeSnapshots stuck `READYTOUSE=false`, `VolSyncVolumeOutOfSync` everywhere | Delete the orphaned `external-snapshotter-leader-*` Lease in `rook-ceph` | Snapshotter logs `Creating snapshot for content`; movers drain over a few hours |
| `coder` 0/1 after a Cilium roll | `kubectl -n default rollout restart deploy/coder` | `/healthz` returns 200 |
| `allocatable.nvidia.com/gpu = 0` on a node after a device-plugin change | Delete that node's `nvidia-device-plugin-daemonset` pod | Allocatable returns to 5 within ~10 s ([KB-014](kb/014-gpu-device-plugin-handover-allocatable-zero.md)) |
| One node's cross-node pod traffic broken after a prolonged outage, host traffic fine | Reboot the **other** nodes, one at a time | That node's spegel pod returns to 1/1 ([KB-008](kb/008-cilium-cross-node-pod-networking-breaks.md)) |

## Deliberately not on this page

**"litellm-operator is chronically under its memory limit."** It was called pre-existing on
2026-09-04 with a suggested bump. Every one of its restarts fell inside the upgrade window and
there were none in the days after. Listing it would pre-authorise dismissing a real leak later.

**"`KubePodNotReady` on litellm is just a slow boot."** It was the **only** signal of a 62-restart
liveness loop that no crashloop alert caught. A vague alert on that pod deserves a `restartCount`
check, every time.

**Ceph mgr or mon memory.** No entry, because no measured baseline exists on this cluster. A mgr
OOM is a novel event here and gets investigated, not waved off.

That is the general test for anything added above: if this entry were wrong, what would it cause
someone to ignore?
