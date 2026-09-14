# Post-change health verdict

The component-agnostic answer to one question after Flux has applied new state: **is the
cluster still healthy?** It does not care which PR landed. Run it after every protected-infra
merge, after a Talos or Kubernetes roll, after a batch of Renovate merges, and whenever an alert
storm needs a second opinion. Component-specific checks live in the
[upgrade playbooks](upgrade-playbooks.md); this page is the layer that runs regardless.

The `health-check` skill under `.agents/skills/` runs the whole thing read-only and reports a
verdict. This page is the design it follows and the reference when a check needs judgement.

## Verdict

| Verdict | Meaning | Action |
| --- | --- | --- |
| **healthy** | Every check green against the prior snapshot | Record the snapshot, done |
| **benign-warn** | A check is non-green but matches a [known noise](../troubleshooting/known-noise.md) entry, or is a transient that clears on re-poll | Note which entry matched, re-poll transients once after 90 s, do not escalate |
| **regression** | A check is non-green and **new**: it broke across the reconcile window, or is tied to the component that changed | Stop merging. Diagnose or roll back per the playbook |
| **blind** | A check could not run (the API refused, a command failed, an empty answer where a populated one is normal) | Say so. Never report a check that did not run as green |
| **baseline** | The first snapshot, with nothing to diff against | Record it and report absolute findings only. `healthy` needs a second snapshot |

Three rules of interpretation:

- **Page only on new.** Pre-existing noise is curated into the known-noise page, not reported
  again each cycle. Diff against the previous snapshot; a firing critical that was firing before
  the change is context, not a regression.
- **A blind check is not a passing check.** The failure mode this exists to prevent is a command
  that errors, is redirected to `/dev/null`, and whose empty output reads as "nothing wrong". Every
  command below prints something when healthy; silence is a failure to run.
- **Re-poll transients before deciding.** Image pulls in the first 90 s, a reconcile still in
  progress, an ExternalSecret that has not had its first refresh. One re-poll, then decide.

## The snapshot

Every run writes one JSON document so the next run has something to diff against. The skill's
`snapshot.sh` produces it; the fields are:

```json
{
  "nodes": {
    "ready": 3,
    "total": 3,
    "cordoned": 0,
    "versions": [
      "v1.37.0"
    ],
    "os": [
      "Talos (v1.14.0)"
    ]
  },
  "flux": {
    "ks_not_ready": [],
    "hr_not_ready": [],
    "src_not_ready": []
  },
  "pods": {
    "not_running": [
      {
        "ns": "<ns>",
        "name": "<pod>",
        "phase": "Failed",
        "created": "2026-09-13T07:30:00Z"
      }
    ],
    "waiting": [],
    "not_ready": [
      {
        "ns": "<ns>",
        "name": "<pod>",
        "since": "2026-09-14T13:00:00Z"
      }
    ],
    "restarts": {
      "<ns>/<pod>": 3
    }
  },
  "eso": {
    "store_ready": true,
    "total": 426,
    "not_synced": []
  },
  "alerts": {
    "watchdog": true,
    "critical": [
      {
        "alertname": "DockerBackupStale",
        "namespace": null,
        "startsAt": "2026-09-09T14:01:15.060Z"
      },
      {
        "alertname": "VMwareHostPoweredOff",
        "namespace": "observability",
        "startsAt": "2026-09-13T12:36:01.885Z"
      }
    ]
  },
  "ceph": {
    "health": "HEALTH_OK",
    "detail": "HEALTH_OK (muted: AUTH_INSECURE_CLIENT_KEY_TYPE AUTH_INSECURE_KEYS_ALLOWED AUTH_INSECURE_KEYS_CREATABLE)",
    "osd": "6 osds: 6 up (since 25h), 6 in (since 10d); epoch: e1177874"
  },
  "volsync": {
    "total": 202,
    "stale": [],
    "synchronizing": [],
    "last_failed": []
  },
  "gatus": {
    "discovered": 132,
    "failing": []
  },
  "taken": "2026-09-14T14:26:36Z"
}
```

That is the real shape (taken from a live run, pod names replaced). `nodes.cordoned` is a count,
not a list of names; `alerts.critical` is a list of objects; `pods.not_ready` is Running pods whose
`Ready` condition is not `True`; `volsync.stale` is sources whose last successful sync is older than
about twice their schedule; `eso` and `volsync` carry a `total`, and `gatus` a `discovered` count of
sidecar-discovered endpoints (the static ones in `config.yaml` would keep a plain total non-zero
with discovery dead), which the script uses as presence guards so a read path that returns
nothing is recorded as blind rather than as empty-and-healthy. A large drop in any of those counts
against the previous snapshot is itself a finding.

Keep snapshots in the session scratchpad during a batch (compare each merge to the one before).
**Never paste a raw snapshot into a PR, an issue or a commit**: this repository is public, and the
snapshot names pods, alerts and namespaces. The script already reduces nodes to counts so a node
name cannot leak through `cordoned`, but pod names can still encode device identities. When the
batch closes, write a redacted summary (the verdict, the counts, the alert names) into the PR.

## Checks

Run cheapest first. All use the repo kubeconfig:

```bash
export KUBECONFIG="$PWD/kubeconfig"   # from the repository root; .mise.toml sets the same
```

### 1. Nodes

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,SCHED:.spec.unschedulable,KUBELET:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage'
```

**Healthy:** three `True`, no `true` under SCHED, one kubelet version, one OS image.
**Regression:** a cordoned node after a roll has finished (tuppr's cordon persists on a false
fail), or mixed versions after the roll reports complete. A node still on the old OS after an
upgrade is the reverted-bootloader case in [Talos upgrades](talos-upgrades.md).

### 2. Flux

```bash
flux get kustomizations -A --status-selector ready=false
flux get helmreleases -A --status-selector ready=false
flux get sources all -A --status-selector ready=false
```

**Healthy:** each prints only its header (or `no ... found`). **Benign-warn:** a reconcile still
in progress seconds after a merge; a burst of dependents behind one failed source, which is
[KB-007](../troubleshooting/kb/007-flux-not-ready-artifact-failed-alert-storms.md) and clears when
the root is reconciled. **Regression:** a HelmRelease at `UpgradeFailed` or `RollbackFailed`, or
a Kustomization failing its health check for longer than one interval. Read the HelmRelease
`status.conditions[].message` before anything else: the apply error is there.

### 3. Pods

```bash
kubectl get pods -A --field-selector='status.phase!=Running,status.phase!=Succeeded' \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,AGE:.metadata.creationTimestamp'
kubectl get pods -A -o json | jq -r '.items[] | .metadata.namespace + "/" + .metadata.name + " " + (((.status.containerStatuses // []) + (.status.initContainerStatuses // [])) | map(.restartCount) | add // 0 | tostring)' | awk '$2 > 0' | sort -k2 -nr | head -20
```

Also list Running pods whose `Ready` condition is not `True` (the script's `not_ready`): a
readiness failure leaves the phase `Running` and the container `running`, so a 0/1 controller or
service without a Gatus endpoint would otherwise be invisible to this check.

**Healthy:** the first list is empty or holds only `Completed` Jobs; no pod is `not_ready` beyond
a rolling restart; the restart list is unchanged from the prior snapshot. Restart counts include
native sidecars under `initContainers` (several apps here run one), which a sum of
`containerStatuses` alone would miss. **Benign-warn:** `ContainerCreating` or `Init` pods under 90 s old;
stale `Error` pods with an old age (GC leftovers, confirm by age and by a healthy running
sibling). **Regression:** any `CrashLoopBackOff` or `OOMKilled` that is new, and **any pod whose
restart count rose since the last snapshot**. The second half is the check the alerts do not
cover: an app that exits cleanly on a liveness kill never enters `CrashLoopBackOff`
([hardening H-5](hardening-backlog.md#h-5-a-probe-killed-graceful-app-is-invisible-to-the-alert-named-after-the-problem)).

### 4. External Secrets

```bash
kubectl get clustersecretstore onepassword-connect -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
kubectl get externalsecrets -A -o json | jq -r '.items[] | select((.status.conditions // []) | map(select(.type=="Ready" and .status=="True")) | length == 0) | .metadata.namespace + "/" + .metadata.name'
```

**Healthy:** `True`, then nothing. **Benign-warn:** a brand-new ExternalSecret inside its first
refresh interval. **Regression:** the store not Ready (every app deploy is now blocked), or an
existing ExternalSecret dropping out of Ready (a renamed 1Password field, an expired Connect
token). A PushSecret that logs HTTP 400 but reports `Synced` is
[KB-001](../troubleshooting/kb/001-1password-connect-pushsecret-false-400-errors.md).

### 5. Alertmanager criticals

```bash
kubectl get --raw "/api/v1/namespaces/observability/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts?active=true&silenced=false&filter=severity%3Dcritical" \
  | jq -r '.[] | .labels.alertname + " " + (.labels.namespace // "-") + " " + .startsAt' | sort
```

The script first confirms the always-firing `Watchdog` alert is active (it is routed to a
heartbeat receiver for exactly this purpose). Without it an empty critical list means Prometheus
has stopped evaluating or delivering, so the check is blind, not quiet.

**Healthy:** Watchdog present and the same critical set as the prior snapshot, or empty.
**Benign-warn:** an entry the [known noise](../troubleshooting/known-noise.md) page covers, named
in the note. **Regression:**
any critical whose `startsAt` is after the change and which is not noise. Remember that
`startsAt` resets on an Alertmanager restart; three alerts sharing one `startsAt` are telling you
when Alertmanager restarted, not when they fired. If the command errors, the check is **blind**:
do not read an empty list as quiet.

### 6. Ceph

```bash
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health detail
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd stat
```

**Healthy:** `HEALTH_OK` (the `muted:` suffix listing `AUTH_INSECURE_*` checks is expected) and
`6 osds: 6 up, 6 in`. **Benign-warn:** `HEALTH_WARN` from recent crash reports of a daemon that
recovered, or degraded PGs recovering within minutes of a node reboot. **Regression:** an OSD
down after the roll is complete, `HEALTH_ERR`, clock skew measured in seconds, or a `HEALTH_WARN`
that persists past one re-poll. The `CephCluster` CR's `status.ceph.health` lags the live command
by a few minutes after reboots; trust the toolbox.

### 7. VolSync

```bash
kubectl get replicationsource -A -o json | jq -r '.items[] | select(.status.conditions[]? | select(.type=="Synchronizing" and .status=="True")) | .metadata.namespace + "/" + .metadata.name'
kubectl get replicationsource -A -o json | jq -r '.items[] | select(.status.latestMoverStatus.result? == "Failed") | .metadata.namespace + "/" + .metadata.name'
```

Then the check the two lists cannot make: a source whose last successful sync is older than about
twice its schedule (the script's `stale`: 9 h for the 4-hourly NFS/Kopia sources, 30 h for the
nightly R2/restic ones). A backup path that quietly stops starting leaves `Synchronizing=False`
and a stale-but-successful `latestMoverStatus` on every source, which reads as healthy.

**Healthy:** both lists empty (or the first holding only sources whose schedule is due now) and
`stale` empty. **Benign-warn:** one source mid-run; a source created within the last schedule
interval. **Regression:** any source in `stale`; a source `Synchronizing` for longer than its interval (a
stale restic lock on an R2 source, see [Talos upgrades](talos-upgrades.md#common-blockers)), every source failing
at once (the snapshotter or the NAS, see
[KB-009](../troubleshooting/kb/009-nfs-mount-failures-host-dns-readonly-export.md) and
[KB-010](../troubleshooting/kb/010-rook-ceph-v120-csi-driver-split.md)), or one app's movers
failing repeatedly ([KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md)).
This is context for the next Talos roll as much as a health signal: tuppr will not start while
any source is `Synchronizing`.

### 8. Gatus

```bash
kubectl get --raw "/api/v1/namespaces/observability/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=gatus_results_endpoint_success%7Bgroup!%3D%22connectivity%22%7D%20%3D%3D%200" \
  | jq -r '.data.result[] | .metric.group + "/" + .metric.name'
```

**Healthy:** empty, or the same set as the prior snapshot. **Benign-warn:** an endpoint for an app
that is mid-roll or scaled to zero on purpose. **Regression:** a newly failing endpoint for an app whose pods say
Running: check the live HTTPRoute hostname before the pod
([KB-020](../troubleshooting/kb/020-httproute-drifts-to-placeholder-hostnames.md)).

## What this deliberately does not check

- **Device and home-automation availability** (Shelly, UPS, BMC, switches). Device health is not
  a deploy signal, and those exporters are already the noisiest part of the alert stream. Their
  alerts appear in check 5 if critical; that is the only path.
- **Application function.** A pod Running and a Gatus 200 do not prove an app works. The
  playbooks name one functional check per component (a Postgres login, a fresh PVC binding, an
  image pulled through spegel). Run those for the component that changed.
- **Anything by pod logs at scale.** Logs are for diagnosing a regression once a check has
  named it, not for finding one.

## Rollback

The verdict is read-and-report. Rolling back is a decision, made per component:

1. Identify the merge: `git log --oneline -- <path>` or the Renovate PR. Check whether the bad
   version is already a known hold in `.renovaterc.json5`.
2. Re-pin the tag (OCIRepository `ref.tag` or the image tag) on a branch, or `git revert` the
   single bump commit. **Do not revert whole files**; the playbooks list the deviations that would
   drop.
3. If the version is permanently bad, add an `allowedVersions` exclusion so Renovate stops
   re-proposing it, in the same PR.
4. Merge, reconcile (`flux reconcile kustomization <name> -n <ns> --with-source`), re-run the
   verdict.

Where `git revert` is not enough, the [upgrade playbooks](upgrade-playbooks.md) say so per
component. The common cases: a Ceph major (one way), a Kubernetes or Talos minor with an etcd
major inside it (one way), a `Cluster` whose Postgres image has been opened by the newer version,
a HelmRelease that needs a suspend and resume to unwedge, and a PVC that has been grown live and
can never shrink back to what git declares.
