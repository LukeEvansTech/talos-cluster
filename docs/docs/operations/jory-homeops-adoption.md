# joryirving/home-ops Adoption Roadmap

**Status:** Planned 2026-07-09 (multi-agent gap analysis + adversarial review, verified against
the live cluster). Nothing below is implemented yet — this page is the pick-up point.

A survey of [joryirving/home-ops](https://github.com/joryirving/home-ops) (`kubernetes/apps/base/llm`
and `.agents`) against this cluster. Most of his stack was already ported (open-webui, litellm,
llmkube, memini, toolhive); this roadmap covers the worthwhile delta, two load-bearing findings
that came out of verification, and the plan to unwind workarounds built on a wrong premise.

## Load-bearing findings

1. **CephFS was never blocked on this cluster.** The June 2026 "Talos ships no ceph kernel
   module" diagnosis was a modprobe-vs-builtin trap — `CONFIG_CEPH_FS=y` is compiled into the
   Talos v1.13.5 kernel and `/proc/filesystems` registers `ceph` on all three nodes today.
   See [KB-024](../troubleshooting/kb/024-cephfs-modprobe-builtin-misdiagnosis.md). This
   invalidates the premise behind the RWO model-storage workarounds (PR G below) and un-blocks
   foreman (see below).
2. **Cross-namespace CiliumNetworkPolicy is the hidden prerequisite for new MCP servers.** The
   `media`, `downloads`, and `home` namespaces only allow ingress from `network` (and, for
   `media`, from `downloads`). Any MCP server pod in `ai` calling those services directly needs
   a new ingress rule on each target namespace (precedent: `allow-litellm-from-consumers` in
   `kubernetes/apps/ai/netpol.yaml`).
3. **Hardware verdict for the "ryzen" question:** the nodes' AMD Ryzen 7000-class CPUs carry a
   2-CU display-class iGPU sharing the CPUs' DDR5 — nothing like the Strix-Halo-class AMD
   hardware jory serves models on (and his `llama-ryzen` runs on a separate utility cluster
   anyway). The iGPU path is rejected. The useful Ryzen angle is **CPU serving of small
   auxiliary models** (Zen 4 AVX-512, large RAM, nodes at 6–8% utilisation), which the cluster
   already proves with `llama-embed` / `llama-rerank` on the plain CPU `llama.cpp:server` image.

## The PR ladder

Ordered so each PR is independently mergeable; nothing after PR A depends on anything before it
except where noted.

| PR  | Contents                                                                | Blockers / prerequisites                        |
| --- | ----------------------------------------------------------------------- | ----------------------------------------------- |
| A   | LLM observability: litellm PrometheusRule, ToolHive telemetry, llama.cpp serving dashboard | none                         |
| B   | Config cherry-picks: SearXNG hardening, litellm `context_window_fallbacks`, add-app SKILL.md wording | none               |
| C   | MCP servers over existing apps: arr + ha (+ optional seerr), with netpol rules | HA token in 1Password; pinnable images   |
| D   | CephFS enablement + comment corrections + RWX smoke test                 | none                                            |
| E   | hermes                                                                   | `hermes-agent` 1Password item; decisions below  |
| F   | (optional) CPU-served ~4B auxiliary model                                | none                                            |
| G   | Model-storage de-workaround (shared CephFS model cache)                  | PR D merged + smoke test green                  |

### PR A — LLM observability

- `kubernetes/apps/ai/litellm/app/prometheusrule.yaml`: port jory's four alerts
  (`LiteLLMFallbackChainExhausted` critical, `LiteLLMModelFailover`, `LiteLLMDeploymentOutage`,
  `LiteLLMAuthOrQuotaFailures`) onto the `litellm_deployment_*` series the existing
  ServiceMonitor already scrapes. Tune thresholds for a single replica, and use
  `absent()` / `for:` windows — several series only appear after a first failure. Consider
  folding in a direct InferenceService-down alert (pod availability on metrics already
  scraped) — the review pass flagged it as a comparable cheap win.
- ToolHive telemetry: an `MCPTelemetryConfig` CR (`prometheus.enabled: true`) in `ai`, plus a
  PodMonitor for the MCP server pods. **Drop jory's `release: kube-prometheus-stack` selector
  label** — this cluster's Prometheus selects monitors without it, and copying it could
  silently break selection.
- llama.cpp serving dashboard (jory's `llama-server.json`) as a GrafanaDashboard, plus
  `targetLabels: [app.kubernetes.io/instance, app.kubernetes.io/name]` on the `llmkube-models`
  ServiceMonitor so panels can filter per backend. Datasource name must be lowercase
  `prometheus` (KB-021). Optionally set `prometheus.inferencePodMonitor.enabled: true` in the
  llmkube HelmRelease (jory has it on; ours is off).

### PR B — config cherry-picks

- SearXNG (`toolhive/mcp-servers/searxng`): disable `autocomplete` and `favicon_resolver`
  (per-keystroke upstream calls trip captcha / rate limits on an automation-driven instance —
  ours is driven by the MCP server and open-webui RAG), add jory's `enabled_plugins` set and
  hostname priority boosting, add a `limiter.toml` with RFC1918 `pass_ip`. Skip the
  Redis-backed limiter, `replicas: 2`, and the Brave API engine.
- litellm `router_settings.context_window_fallbacks`: oversized prompts to the 32k
  `self-hosted` model fall back to the larger-context model instead of erroring.
- `.agents/skills/add-app/SKILL.md`: reword the hardcoded `AskUserQuestion` reference to
  host-neutral phrasing (name it as the Claude Code example). Single line.

### PR C — MCP servers over existing apps

- **arr**: a ToolHive `MCPServer` wired to sonarr/radarr (media) and prowlarr (downloads),
  API keys extracted from the existing 1Password items. Requires new ingress
  CiliumNetworkPolicy rules on **both** `media` and `downloads` (finding 2). Gate: source a
  digest-pinnable image — jory runs it via `npx` at runtime, which violates the image-pinning
  convention here. Register in the `mcp-tools` group and litellm `mcp_servers`.
- **ha**: `MCPServer` for Home Assistant with a **read-only scoped long-lived token** (new
  1Password item) and a new ingress rule on `home`. Treat read-only scoping as mandatory —
  LLM-driven home control is the highest blast radius in this batch.
- **seerr** (optional): the review pass called this the weakest "skip" — same
  exploit-an-existing-app class as arr, nearly free once the `media` netpol rule exists. Add
  read-only as a follow-on if wanted.
- Alternative to all netpol edits: point the MCP servers at the apps' `envoy-internal` gateway
  URLs instead of `.svc` DNS (traffic then originates from `network`, which every namespace
  already allows). Weigh one hop + TLS against three netpol rules.
- Watch the tool budget: litellm's `mcp_semantic_tool_filter` (top_k 8, threshold 0.3) gets
  more low-similarity tools to rank; may need retuning.

### PR D — CephFS enablement

Values-only; never touches the in-use `ceph-blockpool` / `ceph-block` StorageClass. Two files:

1. `kubernetes/apps/rook-ceph/rook-ceph/csi-drivers/helmrelease.yaml`: `drivers.cephfs.enabled: true`,
   mirror the RBD block's snapshotter settings, and set `kernelMountOptions: ms_mode=prefer-crc`
   — this cluster sets `requireMsgr2: true`, and the kernel client needs `ms_mode` to negotiate
   msgr2. Rewrite the stale "no ceph kernel module" comment (KB-024).
2. `kubernetes/apps/rook-ceph/rook-ceph/cluster/helmrelease.yaml`: replace `cephFileSystems: []`
   with a `ceph-filesystem` entry — distinct metadata + `data0` pools (replicated size 3,
   `failureDomain: host`), MDS `activeCount: 1` + `activeStandby: true` (requests ~100m/1Gi,
   limit 4Gi — decide the ceiling), and the `ceph-filesystem` StorageClass. Rewrite the comment
   block. Optionally uncomment the `cephFileSystemVolumeSnapshotClass` (`csi-ceph-filesystem`).

No Talos, schematic, or operator changes. Validate with
`just kube flate-build-hr rook-ceph rook-ceph-cluster`, then after merge: MDS pods Ready,
`ceph -s` HEALTH_OK, and the **gate for PR G** — a throwaway RWX PVC mounted read-write by two
pods on different nodes. The June attempt failed at a layer never pinned down, so the smoke
test is the insurance before anything real depends on CephFS.

### PR E — hermes

Stand up [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) at
`kubernetes/apps/ai/hermes/` as a standard app-template app. All dependencies (litellm, memini,
toolhive) are same-namespace — no new netpol. It idles fine with zero chat platforms, so the
baseline is gateway + dashboard only.

Shape (validated against the open-webui HelmRelease as the structural analog):

- `ks.yaml`: components `volsync`, `dependsOn` litellm + memini + **onepassword-connect**
  (review catch — the ExternalSecret convention), `VOLSYNC_CAPACITY: 10Gi`.
- `app/configmap.yaml` (`hermes-config`): default model `self-hosted` via a `litellm` provider
  (`http://litellm.ai.svc.cluster.local:4000/v1`), memory provider `memini`
  (`MEMINI_URL` pointing at the memini Service, distinct `MEMINI_NAMESPACE`), terminal backend
  `local` with `/opt/data/workspace`, `security.redact_secrets` + `privacy.redact_pii` on.
- `app/externalsecret.yaml`: extract `litellm`, `memini`, and a new `hermes-agent` 1Password
  item (dashboard basic-auth credentials + session secret; bot tokens later).
- `app/helmrelease.yaml`: `fsGroup: 10000` (image-baked UID — not the usual 1000), init
  container bootstraps the pinned memini plugin, gateway + dashboard containers, route on
  `envoy-internal` for `{{ .Release.Name }}.${SECRET_DOMAIN}`, persistence `existingClaim` on
  `/opt/data`. Strip everything jory-specific: Authentik OIDC, the NAS mount, Discord persona,
  code-server sidecar.

Verify before applying (review-pass gaps): the image tag + sha256 digest and container ports
(8642 gateway / 9119 dashboard were research-derived, not confirmed in-repository); memini's
actual Service port; the real ToolHive MCP proxy Service names (the live litellm ConfigMap
disagrees with research snapshots on ports).

Proposed defaults, changeable later — see the decision checklist below for the open ones:
no chat platforms at first boot; MCP wired directly per ToolHive proxy; `approvals.mode:
manual` (hermes gets kubectl/flux/talos tools and a local terminal — the Pod is the only
boundary, so no unattended cluster actions until it has earned trust); reuse
`LITELLM_MASTER_KEY` (open-webui precedent) with a scoped virtual key as later hardening;
`MEMINI_NAMESPACE: hermes`.

### PR F — (optional) CPU-served auxiliary model

A ~4B Q4 model (for example Qwen3-4B-Instruct Q4_K_M) served CPU-only to handle
summaries / classification / drafts without spending an L4 slice. **Mirror the proven
app-template pattern of `llama-embed` / `llama-rerank`** (plain `ghcr.io/ggml-org/llama.cpp`
`server` tag — the CPU build; there is no `server-cpu` tag) rather than the unverified llmkube
`hardware.accelerator: cpu` path. Expect ~10–18 tok/s generation (dual-channel DDR5 bound) —
fine for latency-tolerant work, not for interactive chat. Register in litellm as a distinct
model name and consider pointing litellm's auxiliary tasks at it. Set explicit requests/limits
to protect co-tenants.

### PR G — model-storage de-workaround (requires PR D)

The RWO pattern in `kubernetes/apps/ai/llmkube/models/` exists **only** because of the wrong
June diagnosis (KB-024): per-model `ceph-block` PVCs, one-shot curl staging Jobs carrying
`kustomize.toolkit.fluxcd.io/reconcile: disabled` (Job immutability vs Renovate), and
`modelCache.enabled: false` in the llmkube HelmRelease with the wrong comment. Target state is
jory's, verbatim from his llmkube HelmRelease:

```yaml
modelCache:
  enabled: true
  mode: shared
  accessMode: ReadWriteMany
  storageClass: ceph-filesystem
  size: 200Gi # ours: 100Gi is ample for the current ~27Gi of GGUFs + headroom
```

Steps:

1. Flip `modelCache` in `kubernetes/apps/ai/llmkube/app/helmrelease.yaml` to the block above
   and rewrite its comment.
2. Convert the two Model CRs (`qwen3.6-35b-a3b`, `qwen3-30b-abliterated`) from
   `source: pvc://…` to operator-managed upstream sources so llmkube stages weights into the
   shared cache itself (mirror the Model source syntax in jory's manifests — his
   `memini-summary` Model pulls `unsloth/Qwen3.5-4B-GGUF` directly; verify the exact scheme
   against the chart's CRD docs).
3. Delete the staging Jobs, their `reconcile: disabled` + Checkov-skip annotations, and — after
   cutover is verified — the per-model PVCs. Do **one model at a time**: suspend risk is real
   (KB-015-style HelmRelease timeout thrash if a Recreate rollout wedges), and the old PVC is
   the instant rollback until the cache-served pod is healthy.
4. Keep `--no-mmap` (jory keeps it on CephFS too — cold-fault avoidance). The `--mmproj
   /model-source/mmproj-F16.gguf` path will change with the cache mount — **verify vision still
   works end-to-end** (the loupe app depends on `self-hosted` carrying the mmproj projector).
5. No VolSync on the cache — weights are re-downloadable by design.

What it buys: no staging Jobs or immutability hacks, operator-managed model lifecycle, one
shared weights copy, and any node can serve any model without re-staging — model switching and
failover stop being a re-download event. The `llm-gpu-model` anti-affinity spread stays.

## Foreman: unblocked, parked

foreman + dispatch + foreman-dispatch-bridge is jory's autonomous "GitHub issues in → pull
requests out" pipeline: dispatch grooms and lanes issues with a small local model, the bridge
CronJob claims one ready issue per lane and creates a foreman `Workload`, and foreman's agent
pods (per-language coders → deterministic lint/test gate → read-only reviewer, all inferencing
through litellm) open the PR, with a big-context cloud model as the escalation lane.

Its hard blocker here was the `gateCache` RWX volume — gone once PR D lands. The remaining
question is a soft one: whether local-model coding PRs earn their GPU slices when Claude Code
is the primary agent. Re-evaluate after PRs D + G have proven CephFS in anger. If pursued, his
`GATEPROFILE_MAP` (per-repository lint/build/test commands) must be rebuilt for this account's
repositories, and dispatch needs CNPG + an OIDC story.

## Decision checklist (pick-up point)

- [ ] hermes: which chat surfaces, if any, to enable first (each needs a bot token in 1Password)?
- [ ] hermes: MCP wiring — direct per-proxy entries (default) or litellm's `/mcp` aggregate?
- [ ] hermes: keep `approvals.mode: manual` (default) or `smart`?
- [ ] hermes: image tag to pin (jory runs v2026.7.7; newer exists) + digest.
- [ ] CephFS: StorageClass name (`ceph-filesystem` proposed), MDS memory limit, snapshot class now or later?
- [ ] Model cache size: 100Gi (proposed) or jory's 200Gi?
- [ ] PR C: netpol rules per namespace, or gateway-URL wiring instead?
- [ ] seerr MCP: include in PR C or leave skipped?
- [ ] PR F: worth doing now, and which model?

## Deferred / skipped registry

Deferred (right idea, wrong time): ToolHive `VirtualMCPServer` aggregate (litellm already
aggregates + semantically filters; revisit if external agents need one URL — hermes could be
that trigger), litellm complexity auto-router + cost economics (need a paid cloud roster),
`memini-summary` dedicated model (GPU pressure; PR F could host it on CPU instead), repo-wiki,
speculative decoding, HF-token ExternalSecret.

Skipped with reasons: openclaw + hermes-parallel runtimes as *always-on personas* (hermes is
being adopted deliberately instead), comfyui + miso-gallery + comfyui-mcp (AMD ROCm hardware),
llama-strix / llama-ryzen / llama-vision serving (AMD/Vulkan + DRA on hardware we lack),
toolhive-embed (redundant with all-minilm), memory-mcp (fragments memini), litellm
HA/public/OIDC/ChatGPT deltas (his conventions, not ours), `.agents` foreman + pr-review
instructions (document infrastructure we don't run; our sorting + add-app files verified
better than his), dispatch (pointless without foreman — revisit only together).
