# KB-009: NFS Mount Failures (Host DNS / Read-Only Export)

**Status:** Two distinct causes with different error strings — read the pod event before
acting.

NFS mounts behave differently from in-pod networking, and that trips up triage twice:

1. NFS hostnames are resolved by the **node host resolver**, not CoreDNS.
2. `subPath` directories are created by **kubelet as root**, which NFS root-squashes.

## Symptom

- **Variant A — stuck `Init:0/1` / `PodInitializing`** for a long time, repeating
  `mount.nfs: Failed to resolve server <server>: No address associated with hostname`.
- **Variant B — `CreateContainerConfigError`** with
  `failed to create subPath directory for volumeMount "<name>"`.

## Cause

### Variant A — node host DNS can't resolve the NFS server for new mounts

NFS mounts resolve via the **node host resolver** (Talos hostDNS at `127.0.0.53`), **not**
CoreDNS. When a node's host DNS is failing to resolve the internal split-DNS storage hostname
for *new* mounts, every fresh NFS mount on that node fails — while **pod-level** DNS (CoreDNS)
on the same node resolves the identical name fine, and long-running NFS pods elsewhere are
unaffected (NFS resolves once at mount time and then persists). This correlates with prior
node networking turbulence (a CoreDNS reschedule, see
[KB-008](008-cilium-cross-node-pod-networking-breaks.md)).

A nasty cascade: a backup CronJob with `concurrencyPolicy: Forbid` lets the **one** stuck job
block *every* subsequent run — a single bad node can stall an entire hourly backup chain for
a day. (Data is usually still safe: the primary CNPG barman/S3 base backups + WAL archiving
and the off-site job keep completing; only the supplementary local NFS dump is stalled.)

### Variant B — the NFS base export is read-only / the subPath child is missing

When a pod mounts an NFS **base** export and relies on kubelet to create a `<app>/` subPath
under it, kubelet creates that directory **as root**, which NFS **root-squashes to nobody**.
If the base dataset is exported **read-only** (or the child directory doesn't exist), kubelet
can't `mkdir` the subPath → config error. This commonly surfaces after an unrelated pod
recreation (e.g. a node reboot), but the real cause is a storage-side export change days
earlier.

## Fix

### Variant A — clear the blocked job, then fix the node

```bash
kubectl delete job <stuck-job> -n <ns>
kubectl create job --from=cronjob/<cronjob> <cronjob>-manualfix -n <ns>
```

The fresh pod lands on a healthy node, mounts NFS, and completes in seconds; alerts
auto-clear. That also confirms node-specificity. The **root fix to stop recurrence is
rebooting the affected node** (`talosctl reboot`) to restart its stale host-DNS forwarder —
otherwise future runs that schedule back onto the bad node re-stick.

Quick triage: a backup/NFS pod stuck `PodInitializing` → `kubectl describe pod`, look for
`mount.nfs: Failed to resolve` and **which node**; compare host DNS vs CoreDNS rather than
assuming the hostname/config is wrong.

### Variant B — fix the export storage-side (not in the cluster)

Confirm fast with a busybox debug pod mounting the suspect path and `touch`-testing it, both
as root and as uid 1000 — the tell is **base path `Read-only file system`** (even as the dir
owner) while a **sibling child path is writable**. The fix is on the NAS: create a dedicated
read-write child dataset for the app, owned by the app's uid/gid (e.g. `1000:1000`), cloning
an existing writable share's allowed-networks/maproot. Optionally then point the manifest at
the child path directly instead of base + `subPath`.

**Do not `kubectl delete pod` to "force a remount"** on Variant B — the error is a
write-permission / missing-directory problem, not a stale mount, and deleting the pod can
trigger a Multi-Attach on its RWO block PVC for nothing.

## References

- Kubernetes subPath semantics: <https://kubernetes.io/docs/concepts/storage/volumes/#using-subpath>
- Related: [KB-008](008-cilium-cross-node-pod-networking-breaks.md) (the node turbulence that
  precedes Variant A).
