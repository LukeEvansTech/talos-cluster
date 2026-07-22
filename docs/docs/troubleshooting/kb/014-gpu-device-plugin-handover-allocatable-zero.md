# KB-014: GPU Device-Plugin Handover Leaves `allocatable.nvidia.com/gpu = 0`

**Status:** Resolved; recipe applies to any future device-plugin swap.

## Symptom

After swapping the NVIDIA device-plugin DaemonSet (e.g. standalone →
`gpu-operator`-managed, or a gpu-operator chart migration), some nodes show
`allocatable.nvidia.com/gpu = 0` for **5+ minutes**. Workloads requesting `nvidia.com/gpu` go
`Pending`. The new device-plugin pod logs claim it `Registered device plugin ... with Kubelet`,
but on the affected nodes `talosctl ls /var/lib/kubelet/device-plugins/` shows **no
`nvidia-gpu.sock`** (only `kubelet.sock` + checkpoint), and the checkpoint has
`RegisteredDevices: {}`.

## Cause

A kubelet plugin-manager **handover race**: the old plugin pod's deletion and the new pod's
startup overlap so closely that kubelet's plugin manager loses track of the registration. The
**pod logs lie**: they reflect the in-pod view, not the kubelet-side state. Only some nodes
are affected (whichever lost the race).

## Fix

Restart the device-plugin pod on each affected node. That forces kubelet to observe a fresh
socket creation and clears the stale state:

```bash
kubectl -n gpu-operator delete pod <device-plugin-pod-on-affected-node>
```

The replacement re-registers cleanly (kubelet log: `Got registration request from device
plugin with resource resourceName="nvidia.com/gpu"`), `nvidia-gpu.sock` appears on the host,
and allocatable returns to its expected value (here `5`, with time-slicing) within ~10s. **No
node restart needed.**

## Recipe for any future device-plugin swap

1. Watch post-cutover:
   `kubectl get nodes -o json | jq '.items[].status.allocatable["nvidia.com/gpu"]'`.
2. Any node showing `0` for > 2 min: delete its device-plugin pod and confirm
   `nvidia-gpu.sock` appears at `/var/lib/kubelet/device-plugins/` via `talosctl ls`.
3. **Don't read the plugin pod logs first**: they're misleading. Check the node allocatable
   and the host socket.
4. Don't reach for a kubelet/node restart unless the pod restart fails.

## References

- Kubernetes device-plugin lifecycle:
  <https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/>
