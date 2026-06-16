# KB-004: Talos Patch Rollout Gotchas (TUPPR v0.1.27 / Talos v1.13.0 → v1.13.2)

**Status:** Workarounds documented; root causes not yet fixed. Open items below.

## Symptom

A routine Talos patch upgrade (v1.13.0 → v1.13.2) driven by TUPPR (`talosupgrade/talos` in `kubernetes/apps/system-upgrade/tuppr/`) repeatedly got stuck mid-rollout. The Job's `talosctl upgrade` step completed cleanly (installer ran, new UKI written, set as default boot entry), the drain succeeded, but the **`talosctl reboot --mode=powercycle` step then failed with an RPC timeout** to the cluster-internal `default/talos` Service IP.

The node was left:

- Cordoned (`SchedulingDisabled`).
- Still on the **old** Talos version (kernel + osImage unchanged).
- Drained, so 6 pods (mon-c, osd-X, dragonfly-N, postgres18-N, mosquitto-0, per-volume affinity) sat `Pending` indefinitely.
- Ceph in `HEALTH_WARN` (1 mon + 2 OSDs down) for as long as the cordon stood.

This blocked TUPPR's own health-check (`CephCluster.status.ceph.health == 'HEALTH_OK'`) from passing, so TUPPR refused to create another upgrade Job — a classic chicken-and-egg.

## Cause

The Job pod log shows the install/drain succeed, then:

```text
<node>: node drained
"<node-ip>": rpc error: code = Unavailable desc = connection error: desc =
  "transport: Error while dialing: dial tcp <talos-svc-clusterip>:50000: i/o timeout"
console logs for nodes ["<node-ip>"]:
```

`<talos-svc-clusterip>:50000` is the `default/talos` Service ClusterIP, which proxies to the per-node Talos API. The Service endpoint is hosted on a pod that was itself evicted during the drain — so by the time TUPPR's wrapper tries to issue the powercycle, the Service has no healthy endpoint, the RPC times out, and the wrapper gives up. The node never gets the reboot command.

`bootID` confirms no reboot happened. `kubectl get nodes` shows `LastTransitionTime` weeks-old (no Ready=False transition during the attempt). `kernelVersion` and `osImage` stay on v1.13.0.

## Fix

### Option A: Manual `talosctl reboot --mode=powercycle` (recommended)

The install is already staged — `sd-boot: using Talos-v1.13.X.efi as default entry` is already in the META partition. A direct reboot from the operator's host bypasses the broken in-cluster API path:

```bash
export TALOSCONFIG=./talos/clusterconfig/talosconfig
talosctl --nodes <node-ip> reboot --mode=powercycle
```

`talosctl` talks to the node directly (not through the cluster Service), so it survives the drain that broke the in-cluster path. Node boots into the new version, kubelet re-registers, Pending pods schedule back. Run `kubectl uncordon <node>` afterwards — TUPPR's cordon does not auto-clear when bypassed.

### Option B: Break the deadlock with `kubectl uncordon`, then let TUPPR retry

If you'd rather TUPPR drive the rollout:

```bash
kubectl uncordon <node>
```

Volume-pinned pods schedule back, Ceph recovers to `HEALTH_OK` within ~60-90 s, TUPPR's pre-flight health-check passes, and TUPPR creates a fresh upgrade Job. **Caveat**: the same RPC-timeout failure pattern often recurs on the retry. For one node the recipe eventually worked after 1-2 retries; for another it failed 3x in a row and we fell back to Option A.

### Related cleanup: stale CSI VolumeAttachments after drain

After multiple drain/retry cycles on the same node, `VolumeAttachment` records to the original node may persist with `ATTACHED=true` even though no pod is using them. They block new attachments on the destination node ("Multi-Attach error" → "AttachVolume.Attach failed... volume attachment is being deleted"). `kubectl delete volumeattachment <name>` hangs on the `external-attacher/rook-ceph.rbd.csi.ceph.com` finalizer because the CSI driver doesn't confirm detachment.

Force-clear the finalizer:

```bash
kubectl patch volumeattachment <csi-attachment-name> \
  --type=merge -p '{"metadata":{"finalizers":null}}'
```

Safe when the originating pod is already gone (the RBD kernel mapping is cleaned up on pod termination; only the API-level record is stale).

### Related cleanup: KEDA `nfs-scaler` flap during the rollout

When the `blackbox-exporter-lan` pod gets shuffled by the drain, the fresh pod's blackbox probe to the NFS server on port 2049 returns `probe_success=0` (with `probe_ip_addr_hash 0` — DNS resolve fails inside the pod even though the same lookup works from `kubectl run --image=netshoot` in the same namespace). This trips the `components/nfs-scaler` KEDA `ScaledObject`, which scales NFS-dependent deployments (notably Plex) to `0` even though NFS is genuinely reachable.

Restarting the blackbox-exporter pod usually clears it:

```bash
kubectl delete pod -n observability -l app.kubernetes.io/name=prometheus-blackbox-exporter
```

If KEDA keeps flapping during recovery, pause it on the affected ScaledObject:

```bash
kubectl annotate scaledobject -n <ns> <name> \
  autoscaling.keda.sh/paused=true \
  autoscaling.keda.sh/paused-replicas=1 \
  --overwrite
```

Note: Flux may strip these annotations on the next HelmRelease reconcile if they're not in the chart values — re-apply if KEDA scales the workload back to 0.

### Open items

1. The TUPPR Job should not route its post-drain `reboot` call through a cluster-internal Service whose endpoint may have been evicted by the drain. Worth raising upstream or pinning `rebootMode: kexec` to skip the powercycle path.
2. The `blackbox-exporter-lan` pod's intermittent DNS-resolve failure on target hostnames needs investigation — likely a `dnsPolicy` / resolv.conf issue post-Cilium-identity-shuffle.
3. KEDA-on-Plex pause annotations get reverted by Flux; it would be cleaner to set them through chart values so they survive reconcile.

## References

- TUPPR: <https://github.com/home-operations/tuppr>
- KEDA `paused-replicas` semantics: <https://keda.sh/docs/latest/concepts/scaling-deployments/#pause-autoscaling>
