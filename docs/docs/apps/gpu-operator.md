# GPU operator

## Purpose

`kubernetes/apps/gpu-operator/gpu-operator` runs the NVIDIA GPU operator (device discovery,
dcgm-exporter metrics, CDI) alongside the standalone `dra-driver-nvidia-gpu` chart. See
[GPU time-slicing → Dynamic Resource Allocation](../migrations/gpu-time-slicing-to-dra.md) for the
device-plugin-vs-DRA split this operator is configured for, and why its Kustomization health check
matters to that migration.

## DRA metrics need extra plumbing

Setting `dcgmExporter.env.KUBERNETES_ENABLE_DRA=true` is not enough on its own to get pod labels
onto GPU metrics. The operator ships the dcgm-exporter DaemonSet with
`automountServiceAccountToken=false`, and only overrides that when `enablePodLabels`,
`enablePodUID`, or a real `DCGM_EXPORTER_CONFIGMAP_DATA` is set (unverified against upstream
source: `controllers/object_controls.go`, function `dcgmExporterEnvRequiresAPIAccess`). Without the
token, the exporter logs "pod labels will not be available" and every GPU metric loses
`exported_pod`/`exported_namespace`, labels the per-pod VRAM panels in the `ai/llmkube` NVIDIA
dashboard query directly. `enablePodLabels: true` restores the token.

With pod labels on, dcgm-exporter still needs to resolve GPU to pod through `resource.k8s.io`
objects instead of the retired device-plugin pod-resources view, and the operator's own
ServiceAccount carries no such access. `app/rbac.yaml` adds a ClusterRole/ClusterRoleBinding
(`nvidia-dcgm-exporter-dra`) granting `get`/`list`/`watch` on `deviceclasses`, `resourceclaims`,
`resourceclaimtemplates`, and `resourceslices`. Without it, every metric loses its pod, namespace
and container labels and the GPU dashboard panels go blank.

## Alert design: excluding an inference node from GpuHighMemoryUsage

`GpuHighMemoryUsage` excludes the node running an llmkube InferenceService. `llama.cpp` holds its
weights and KV cache resident by design, sitting at about 92% of the card whether or not a request
is in flight. That card's real failure mode, CUDA OOM killing the server, is covered by
`InferenceServiceDown` and pod restart alerting instead.

Excluding by `exported_container!="llama-server"` does not work, and only looked like it did while
the runtime pod was the card's sole GPU tenant. The card is time-sliced, so DCGM attributes its
total `FB_USED` to exactly one co-tenant pod at a time, and alternates: over 24 hours the same
20.7GiB was stamped with the runtime pod for 96 of 288 samples and with an unrelated transcoder pod
for the other 192, mutually exclusive, never concurrent (#4565). When the alert fires, nothing in
the series identifies the real owner, and no label matcher on the metric itself can tell them apart.

Excluding by node is therefore the only correct scope. The node is derived from the llmkube runtime
pod label rather than named in the rule, so the exclusion follows the InferenceService if it is
renamed or moved. The 1h `max_over_time` keeps the exclusion asserted across a runtime pod restart,
which would otherwise re-open the flap. Every other GPU node still alerts normally. Verified live:
the series count dropped from 3 to 2 once the exclusion took effect.
