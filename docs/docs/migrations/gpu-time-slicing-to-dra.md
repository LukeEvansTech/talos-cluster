# GPU time-slicing → Dynamic Resource Allocation

Replaces the NVIDIA device plugin's time-slicing configuration with Dynamic Resource
Allocation, served by the standalone
[`dra-driver-nvidia-gpu`](https://github.com/kubernetes-sigs/dra-driver-nvidia-gpu) chart
running alongside the existing gpu-operator.

Capacity does not change. The five time-sliced replicas per node become five consumable
shares per node, so the only thing this migration moves is the allocator.

!!! warning "This has a drain boundary"
    The device plugin and the DRA kubelet plugin keep independent ledgers. While both
    advertise the same physical card they can allocate it twice. GPU workloads must be
    stopped for the swap, in both directions.

## What this does and does not buy

**Does:** a workload describes the device it wants through a `ResourceClaimTemplate`
instead of asking for an opaque fifth of a card. That is the precondition for any later
per-workload differentiation — a dedicated card for inference, VRAM accounting, selecting a
device by attribute.

**Does not:** VRAM isolation. The card reports `nvidia.com/mig.capable=false`, so sharing is
still time-slicing underneath and a workload can exhaust the memory its co-tenants need. DRA
changes how a device is requested and accounted, not how it is partitioned.

## Components

| Component | Role |
| --- | --- |
| `dra-driver-nvidia-gpu` | Kubelet plugin DaemonSet publishing the `gpu.nvidia.com` ResourceSlice and preparing CDI specs |
| `gpu-operator` | GPU feature discovery, dcgm-exporter, validator, CDI. **Device plugin disabled** |
| Talos system extensions | Driver and container toolkit; driver root `/usr/local` |
| `any-nvidia-gpu` | Namespace-scoped `ResourceClaimTemplate` in `media`, referenced by every GPU pod there |

Everything stays in the `gpu-operator` namespace. The upstream migration this follows moved
to a new privileged namespace to satisfy Pod Security Admission, which forced a manual Helm
uninstall to escape the `meta.helm.sh/release-namespace` annotation on cluster-scoped
objects. This cluster removes the PodSecurity admission plugin in
`talos/patches/controller/cluster.yaml` and the namespace carries no PSA labels, so that
work buys nothing here and is deliberately skipped.

## Invariants

**Exactly one whole-GPU advertiser per node.** Three values flip together and must never
diverge:

```text
gpu-operator:           devicePlugin.enabled=false
dra-driver-nvidia-gpu:  resources.gpus.enabled=true
dra-driver-nvidia-gpu:  gpuResourcesEnabledOverride=true
```

`gpuResourcesEnabledOverride` is only an acknowledgement gate in the chart's
`templates/validation.yaml`. It exists so a stray `gpus.enabled=true` fails the install
rather than silently dual-advertising.

The driver's Kustomization `dependsOn` `gpu-operator`, and `gpu-operator`'s Kustomization
carries a HelmRelease health check so that dependency means "the operator reconciled" rather
than "the manifests applied". **Treat that as defence in depth, not the guarantee.** The
operator removes the device plugin DaemonSet asynchronously after the Helm upgrade returns,
so Flux ordering alone cannot prove the plugin is gone. The drain below is what makes the
swap safe.

**Driver root.** `nvidiaDriverRoot: /usr/local`, because Talos installs the driver as a
system extension rather than at host root. This is the whole reason for the standalone chart:
gpu-operator's `GPUCluster` CR derives the root from the validator's driver-ready contract,
whose pre-installed-driver branch hardcodes `/`, and there is no values-level fix.

**Sharing.** The device plugin's `replicas: 5` has no equivalent in the driver's
`sharing.strategy`, which only sets the CUDA time-slice duration. Oversubscription comes from
the `ConsumableShares` feature gate with `consumableShares: 5` — the literal equivalent of
the old replica count.

### Why not `consumableShares: memory`

VRAM accounting looks like the obvious upgrade, and it was evaluated properly on 2026-09-20
and rejected. Recording the reasoning so it is not re-proposed from first principles.

Read `cmd/gpu-kubelet-plugin/consumable_shares.go` rather than the prose: **`memory` does not
add accounting on top of the share ceiling, it replaces it.** Only the numeric branch publishes
`dev.Capacity["shares"]` at all. The `memory` branch sets the memory request policy to
`Default: maxMem`, so a claim that omits an explicit request consumes the **whole device**.

Three consequences, in increasing order of seriousness:

- The five `media` apps share one `ResourceClaimTemplate`, so they would all need the same
  explicit `capacity.requests.memory`.
- **That figure cannot be measured here.** Without MIG there is no per-process attribution:
  `DCGM_FI_DEV_FB_USED` reports the device total and the exporter attributes that same total to
  every co-tenant. Any value chosen is an estimate presented as a measurement.
- **It converts a soft risk into a hard one.** Today a media pod landing on the inference card
  is harmless to scheduling — it takes one of five shares and the model still fits. Under
  `memory` the same event makes the model *unschedulable*, because it needs the whole device.
  Pod anti-affinity is evaluated against running pods, so the model's `Recreate` rollout gap is
  precisely when a restarting media pod could claim that card and leave it `Pending`.

Closing that race needs a reservation that survives the pod being absent — a node taint or
label — which on Talos is a machine-config change with a possible reboot, and pins the model to
one node. Disproportionate, because the outcome it buys is already achieved: the preferred
`podAntiAffinity` added in #5308 puts the model alone on its card, with the two transcoder
cards at ~22565 MiB free.

Revisit if a second heavy VRAM consumer appears, or if the hardware gains MIG.

`unlimited` removes the per-node ceiling entirely and is not wanted here: spreading would then
rest only on `topologySpreadConstraints`, which are `ScheduleAnyway` and therefore advisory.

## Workload pattern

```yaml
controllers:
  main:
    pod:
      resourceClaims:
        - name: gpu
          resourceClaimTemplateName: any-nvidia-gpu
    containers:
      app:
        resources:
          claims:
            - name: gpu
```

Each pod gets its own generated `ResourceClaim`. `runtimeClassName: nvidia` and the
`nvidia.com/gpu.present` node affinity stay as they are — GFD still publishes those labels,
and the runtime class is how CDI injection reaches the container.

`ResourceClaimTemplate.spec` is immutable. Changing the request later needs `force: true` on
the Kustomization or a one-time manual delete.

### The llmkube exception

The GPU inference service is an `inference.llmkube.dev/v1alpha1` `InferenceService`. Its CRD
has no `resourceClaims` field — `spec.resources` offers only `cpu`, `memory`,
`ephemeralStorage`, `gpu`, `gpuMemory`, `gpuSharing` and `hostMemory` — so it renders
`nvidia.com/gpu` into the pod spec and cannot be made to emit a claim.

It keeps working through extended-resource bridging. The chart's `gpu.nvidia.com` DeviceClass
carries `extendedResourceName: nvidia.com/gpu`, and with `DRAExtendedResource` GA on
Kubernetes 1.37 the scheduler satisfies that request from DRA and generates an implicit
claim. Its manifest is unchanged by this migration.

This is the one place bridging is used. Everything else requests a claim explicitly.

## Cutover

Do these in order. Steps 1 and 3 are what actually make the swap safe; the Flux `dependsOn`
is a backstop, not a substitute.

### Step 1 — suspend every GPU consumer

Use `flux suspend`, not a Git edit, so the repository and the cluster do not disagree
mid-flight.

```sh
flux suspend ks jellyfin plex tdarr pinchflat dispatcharr -n media
flux suspend ks llmkube -n ai
kubectl -n media scale deploy jellyfin plex tdarr pinchflat dispatcharr --replicas 0
```

Confirm no GPU pods remain before continuing.

### Step 2 — merge and reconcile

```sh
flux reconcile source git flux-system
flux reconcile ks cluster-apps
```

### Step 3 — confirm the device plugin is gone

Allocatable must read `0` on every node:

```sh
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
```

!!! note "`0` is the signal, not absence"
    Kubelet deliberately keeps a departed device plugin's resource key with value `0` rather
    than deleting it. Capacity follows after a five-minute grace period, and the key itself
    only disappears on a kubelet restart. Waiting for it to vanish will hang.

### Step 4 — wait for the ResourceSlice

One per node, published by the `gpu.nvidia.com` driver:

```sh
kubectl get resourceslices
kubectl get resourceslice -o yaml | grep -A3 allowMultipleAllocations
```

### Step 5 — resume the five media consumers

```sh
flux resume ks jellyfin plex tdarr pinchflat dispatcharr -n media
kubectl get resourceclaims -n media
```

Every claim should be `allocated` and carry a `shareID`.

### Step 6 — resume the inference service last

```sh
flux resume ks llmkube -n ai
kubectl get resourceclaims -n ai
```

This is the step that tests extended-resource bridging. It is deliberately last because it
is the least certain and the most disruptive to leave down. If the pod stays `Pending` on
insufficient `nvidia.com/gpu`, the bridge is not working: raise `consumableShares`, or give
the model its own node using the `podAntiAffinity` it already carries.

### Step 7 — check observability

dcgm-exporter metrics should still carry `pod`, `namespace` and `container` labels, and the
GPU dashboard panels should populate. If they are blank, check the
`nvidia-dcgm-exporter-dra` ClusterRoleBinding.

Then run the [health verdict](../operations/health-verdict.md) and compare against the
previous snapshot.

## Rollback

Reverse the same boundary. A partially-up DRA driver must be drained, not just reverted.

```sh
# 1. stop the consumers
flux suspend ks jellyfin plex tdarr pinchflat dispatcharr -n media
flux suspend ks llmkube -n ai
kubectl -n media scale deploy jellyfin plex tdarr pinchflat dispatcharr --replicas 0

# 2. confirm no allocated gpu.nvidia.com claims remain
kubectl get resourceclaims -A

# 3. revert the commit, then reconcile
flux reconcile source git flux-system
flux reconcile ks cluster-apps

# 4. wait for the ResourceSlice to go and nvidia.com/gpu to return non-zero
kubectl get resourceslices
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'

# 5. resume the consumers
flux resume ks jellyfin plex tdarr pinchflat dispatcharr -n media
flux resume ks llmkube -n ai
```

Rollback here is cheaper than upstream's because the namespace never moves, so there is no
Helm release to re-adopt.

## Known gotchas

- **Stale node labels.** GFD keeps publishing `nvidia.com/gpu.replicas` and
  `nvidia.com/gpu.sharing-strategy` from the device plugin's view until it resyncs. Nothing
  in this repository selects on them; the consumers select on `nvidia.com/gpu.present`, which
  stays correct.
- **Two unused DeviceClasses.** The chart also creates `mig.nvidia.com` and
  `vfio.gpu.nvidia.com`. Both are inert on a card that reports `mig.capable=false`.
- **The chart is unsigned.** `registry.k8s.io` publishes no cosign signature for it, so the
  `OCIRepository` has no `verify` block.
- **`consumableShares` is a plain env var** (`CONSUMABLE_SHARES`) on the kubelet plugin, so
  changing it is a HelmRelease edit and a DaemonSet roll, not a cluster-wide change.
