# AI / LLM stack

Self-hosted LLM stack in the `ai` namespace, fronted by **LiteLLM**. Patterns adapted from
[joryirving/home-ops](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm),
re-targeted to this cluster — **NVIDIA L4 GPUs + Ollama** instead of his AMD/Mac/llama.cpp gear.

## Components

| App          | Role                                                               | Status        |
| ------------ | ------------------------------------------------------------------ | ------------- |
| `litellm`    | OpenAI-compatible gateway: routing, fallbacks, cache, metrics, MCP | live          |
| `ollama`     | local inference (3× L4, one per node); embeddings + general chat   | live          |
| `open-webui` | chat UI                                                            | live          |
| `toolhive`   | MCP servers (kubectl/flux/talos/searxng) wired into LiteLLM        | live          |
| `memini`     | agent long-term memory (SQLite + CPU embed/rerank)                 | live          |
| `llmkube`    | llama.cpp model-serving operator (CUDA); model template staged     | operator live |

LiteLLM persists to CNPG `postgres18` (`litellm` db) and caches in Dragonfly. Internal-only route
(`litellm.${SECRET_INTERNAL_DOMAIN}`, envoy-internal).

## Ollama vs llama.cpp

Not either/or — **pick per model, LiteLLM fronts both**:

- **Ollama** = the "it just works" tier: embeddings, zero-fuss model pulls, auto model-swap,
  keep-alive. Already running on 3 nodes. Default backend for `self-hosted` and embeddings.
- **llama.cpp** (Layer 4, via llmkube) = the tuned tier for the 1–2 hot models where the knobs pay
  off on a 24 GB L4: KV-cache quant (`q8_0` ≈ 2× context), speculative decoding, flash-attn,
  reasoning budgets, per-model `llamacpp_*` metrics. Cost: you own GGUF staging + one model/server.

LiteLLM model-**groups** make this transparent: several backends share one client-facing
`model_name` with an `order:`, and LiteLLM load-balances / fails over across them. So Layer 4 adds
llama.cpp backends to the existing `self-hosted` group with no client change.

## Ollama model provisioning

Models are **provisioned declaratively** by a GPU-less `provisioner` sidecar in the Ollama
StatefulSet — the ollama image used as a CLI client against the server in the same pod. It
reconciles the declared set (`kubernetes/apps/ai/ollama/app/resources/`: `models.list` plus
`*.Modelfile`, bundled into the `ollama-models` ConfigMap) onto **each** replica's RWO
`/models` PVC, so the set is identical across the fleet and a fresh PVC self-heals. The loop is
idempotent and never exits — a crash would flip the pod NotReady and drop the server from the
Service.

Declared today: `qwen3.6:27b`, `qwen3.6-35b-a3b` (the default, `OLLAMA_MODEL` → `self-hosted`),
and `qwen3-30b-a3b-abliterated` — mradermacher's imatrix GGUF of mlabonne's Qwen3-30B-A3B
abliterated (Q4_K_S ≈ 17.5Gi), exposed via LiteLLM as `self-hosted-uncensored` with **no** cloud
fallback (a cloud model would reintroduce refusals). Two ~17Gi models can't co-reside on one 24Gi
card, so alternating between the default and the uncensored model triggers a model swap
(~seconds) — fine for ad-hoc use. (The exact huihui-ai 3.6-35B-A3B abliterated GGUF is avoided —
its bare `Q3_K`/`Q4_K` file tags aren't valid Ollama quant schemes.) The abliterated model sets
`PARAMETER num_ctx 8192` (2× Ollama's 4096 default) so it's usable from opencode/agents; it loads
~20Gi 100% on-GPU, preserving headroom for the time-sliced Plex/Jellyfin transcodes on the same
L4. The reconciler **always** re-runs `ollama create` for Modelfiles so such `PARAMETER` changes
converge (the change applies on the next model load — keep-alive expiry, eviction, or
`ollama stop`).

To add a model: drop a `<name>.Modelfile` (or a `models.list` line) under `app/resources/`, add
it to the `configMapGenerator` in `app/kustomization.yaml`, and commit — Reloader restarts the
pods and the reconciler pulls/creates it. `kubectl -n ai rollout restart statefulset/ollama`
applies it immediately. The generator sets `kustomize.toolkit.fluxcd.io/substitute: disabled` so
Flux postBuild doesn't empty the `${...}` shell vars in `reconcile.sh`.

## Rollout (staged)

1. **LiteLLM uplift** — model-groups + router fallbacks + commented hooks for MCP / embeddings /
   cloud providers.
2. **ToolHive + MCP** — operator + curated MCP servers (kubectl, flux, talos, searxng) wired into
   LiteLLM `mcp_servers`. (VirtualMCPServer aggregate + semantic filter deferred — see below.)
3. **memini** — agent memory; embeddings + rerank via tiny CPU llama.cpp servers, consolidation via
   LiteLLM.
4. **llmkube** — operator installed; a CUDA `Model` + `InferenceService` template is documented
   below (running it needs GPU headroom from Ollama + model storage). ComfyUI image gen skipped
   (also GPU-bound, AMD-only upstream).

Each layer is a separate commit on one branch (one PR). `mcp_servers` and
`mcp_semantic_tool_filter` are now fully active in `litellm/app/configmap.yaml`; only the optional
cloud-provider stubs remain commented out.

> Not ported from Jory's repository: `hermes`, `openclaw` (agent runtimes), and `agentmemory` (whose main
> consumers are hermes/openclaw).

## How to extend LiteLLM

- **Add a backend to a group** — add a `model_list` entry with an existing `model_name` and the
  next `order:`. LiteLLM balances / fails over within the group.
- **Add a cloud provider** — add the key to the `litellm` 1Password item, add a line to
  `externalsecret.yaml`'s `target.template.data`, then uncomment the matching stub in
  `configmap.yaml`. Don't reference an `os.environ/KEY` that isn't in the secret — the pod env read
  fails at startup.
- **Fallbacks** — `router_settings.fallbacks` is a list of `{model_name: [fallback, …]}`.

## Consuming the stack from a workstation (opencode)

Any OpenAI-compatible client can drive the self-hosted models through the gateway — internal route
`https://litellm.${SECRET_INTERNAL_DOMAIN}/v1`, models `self-hosted` and `self-hosted-uncensored`.
[opencode](https://opencode.ai) is wired this way as a custom provider:

- **Provider** — `@ai-sdk/openai-compatible`, `baseURL: https://litellm.${SECRET_INTERNAL_DOMAIN}/v1`.
  Put the LiteLLM key in the client's own credential store (opencode: `opencode auth login` →
  `~/.local/share/opencode/auth.json`), **never** as a literal in a shared or committed config.
- **MCP tools** — point a remote MCP server at LiteLLM's MCP gateway,
  `https://litellm.${SECRET_INTERNAL_DOMAIN}/mcp/` (the `litellm-mcp-server`), with the LiteLLM key
  as `Authorization: Bearer …`. Curate the server set with the `x-mcp-servers` header (e.g.
  `kubectl,flux,talos,searxng`) — requesting **all** servers times out, and the full tool list
  bloats every request (heavy on the small-context local models, so prefer a frontier model for
  tool-heavy work).
- **Gotchas** — the self-hosted models are Qwen3 *thinking* models (send `think: false` for
  non-reasoning output); the uncensored model runs at `num_ctx 8192`; switching between the two
  local models forces a ~17Gi VRAM model-swap (seconds, longer on a cold replica).

Prefer a **scoped LiteLLM virtual key** (`/key/generate`, limited to the `self-hosted*` models) over
the master key for any workstation client — it's revocable on its own.

## MCP tools (ToolHive)

Layer 2 runs the [StackLok ToolHive](https://github.com/stacklok/toolhive) operator (`0.29.3`,
separate CRDs + operator charts) in `ai`, an `MCPGroup` (`mcp-tools`), and these MCP servers, all
wired into LiteLLM's `mcp_servers`:

| Server    | Source                          | Access                                                  |
| --------- | ------------------------------- | ------------------------------------------------------- |
| `kubectl` | kubectl-mcp-server              | cluster read-only, secrets excluded                     |
| `flux`    | flux-operator-mcp               | Flux read-only (write is opt-in)                        |
| `talos`   | talos-mcp                       | Talos `os:reader` (talosconfig SA)                      |
| `searxng` | mcp-searxng → `searxng.default` | web search                                              |
| `github`  | github-mcp-server               | GitHub read-only (`GITHUB_READ_ONLY`, fine-grained PAT) |
| `grafana` | grafana/mcp-grafana             | Grafana Viewer SA token (read-only)                     |

kubectl + flux share one read-only `ClusterRole` (`kubectl-mcp-readonly`) built from this cluster's
API groups with core `secrets` omitted — keep it in sync with `kubectl api-resources` as you add
CRDs. The talos MCP mounts a `talos.dev` `ServiceAccount`-minted `os:reader` talosconfig.

The `mcp_semantic_tool_filter` is **on** (top_k 8, embeddings via the `all-minilm` model on the
CPU `llama-embed` pod): with ~110 tools across 6 servers it trims each request to the most
relevant tools. `github` + `grafana` are read-only — a fine-grained PAT (`toolhive-github`) and a
Grafana Viewer service-account token (`toolhive-grafana`).

Deferred (add later): the `VirtualMCPServer` aggregate + `EmbeddingServer` (a single
`mcp.<domain>` endpoint for non-LiteLLM clients — it's what pulls in a Dragonfly + embedder).

### Enabling flux-mcp write access

The flux MCP is read-only by default. To let it — and therefore any model behind LiteLLM —
reconcile / suspend / resume / apply / delete Flux objects, append to
`toolhive/mcp-servers/flux/rbac.yaml` (no `kustomization.yaml` change needed — `rbac.yaml` is
already listed there):

```yaml
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
    name: flux-mcp-write
rules:
    - apiGroups:
          - fluxcd.controlplane.io
          - helm.toolkit.fluxcd.io
          - image.toolkit.fluxcd.io
          - kustomize.toolkit.fluxcd.io
          - notification.toolkit.fluxcd.io
          - source.toolkit.fluxcd.io
      resources: ["*"]
      verbs: ["create", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
    name: flux-mcp-write
roleRef:
    apiGroup: rbac.authorization.k8s.io
    kind: ClusterRole
    name: flux-mcp-write
subjects:
    - kind: ServiceAccount
      name: flux-mcp
      namespace: ai
```

This grants an LLM mutate access to the cluster's GitOps controller — enable only if you trust the
calling chain.

### Adding an MCP server

Drop an `MCPServer` (operator-managed) or `MCPServerEntry` (remote URL) with `groupRef: mcp-tools`
under `toolhive/mcp-servers/<name>/`, list it in that dir's `kustomization.yaml`, then add its
endpoint to LiteLLM's `mcp_servers`. The service name depends on the transport:

- **Native `streamable-http` transport** (e.g. kubectl, flux): ToolHive creates `mcp-<name>` on the
  spec's `mcpPort`.
- **`stdio` transport with `proxyMode: streamable-http`** (e.g. github, grafana): ToolHive creates
  `mcp-<name>-proxy` on the spec's `proxyPort` (typically 8080).

## Agent memory (memini)

Layer 3 runs [memini](https://github.com/eleboucher/memini) (SQLite backend) for agent long-term
memory, plus two tiny CPU `llama.cpp` model servers in `ai`:

- `llama-embed` — all-MiniLM-L6-v2 (384-dim), `--embeddings`, OpenAI `/v1`.
- `llama-rerank` — Qwen3-Reranker-0.6B, `--rerank`.

Both are the same models Jory serves, but run on **CPU** (`ghcr.io/ggml-org/llama.cpp:server`,
GPU/Vulkan bits stripped) — the L4s are spoken for by Ollama and these models are small (~30 MB /
~600 MB). memini's consolidation LLM is LiteLLM's `self-hosted` group (→ Ollama).

Secrets: a generated `MEMINI_API_KEY` (Talos vault item `memini`) + `LITELLM_MASTER_KEY` (reused
from the `litellm` item). Data PVC via the volsync component (10Gi). Route:
`memini.${SECRET_INTERNAL_DOMAIN}` (internal).

To move embeddings onto the GPU later, swap `llama-embed`/`llama-rerank` for llmkube
`InferenceService`s (Layer 4) and repoint `MEMINI_EMBED_BASE_URL` / `MEMINI_RERANK`.

## Layer 4 — llama.cpp serving (llmkube)

The [llmkube](https://github.com/defilantech/LLMKube) operator (`0.8.7`) is installed in `ai`. It
serves llama.cpp models declaratively: a `Model` CR (weights source + hardware) plus an
`InferenceService` CR (the serving pod). **No model runs by default** — two prerequisites must be
met first:

1. **GPU memory.** Each L4 (24 GB) is already ~17 GB into Ollama. A 27B llama.cpp model needs
   another ~17 GB, which won't fit alongside Ollama on the same card (time-slicing shares compute,
   not memory). To run one, scale Ollama down (fewer replicas / smaller model / lower
   `OLLAMA_NUM_PARALLEL`) or choose a small model that fits the ~5–6 GB headroom.
2. **Model storage.** This cluster has only RWO `ceph-block`; llmkube's shared download cache and
   `pvc://` staging assume RWX (`ceph-filesystem`). A single `InferenceService` works on an RWO
   `ceph-block` PVC (one pod); for an `https://` source set `modelCache.enabled: true` in the
   operator HR and give it a cache PVC.

### A CUDA model template

Drop this under `llmkube/models/` (its own Flux Kustomization, `dependsOn: llmkube`) and add the
path to `ai/kustomization.yaml`. `replicas: 0` keeps it staged until you scale it up:

```yaml
---
apiVersion: inference.llmkube.dev/v1alpha1
kind: Model
metadata:
    name: qwen3.6-27b
spec:
    source: https://huggingface.co/<org>/<repo>-GGUF/resolve/main/<file>.gguf # direct GGUF URL
    format: gguf
    quantization: UD-Q4_K_XL
    hardware:
        accelerator: cuda
        gpu: { enabled: true, vendor: nvidia, count: 1, layers: 99 }
---
apiVersion: inference.llmkube.dev/v1alpha1
kind: InferenceService
metadata:
    name: llama-nvidia
spec:
    modelRef: qwen3.6-27b
    runtime: llamacpp
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    replicas: 0 # scale to 1 once GPU headroom exists
    runtimeClassName: nvidia
    contextSize: 131072
    flashAttention: true
    cacheTypeK: q8_0
    cacheTypeV: q8_0
    resources: { gpu: 1, cpu: "500m", memory: 16Gi }
    endpoint: { port: 8080, type: ClusterIP }
```

Then add it to LiteLLM's `self-hosted` group (in `litellm/app/configmap.yaml`) as `order: 2`:

```yaml
- model_name: self-hosted
  litellm_params:
      model: openai/llama-nvidia
      api_base: http://llama-nvidia.ai.svc.cluster.local:8080/v1
      api_key: llama-nvidia
      order: 2
```

LiteLLM then load-balances `self-hosted` across Ollama (order 1) and llama.cpp (order 2), failing
over automatically — no client change.

## Gotchas

- **Public repository** — no LAN IPs / internal hostnames in Git (see `CLAUDE.md`). Cluster service DNS
  and `${SECRET_*}` placeholders are fine.
- **Metrics** — `require_auth_for_metrics_endpoint: false` **and** ServiceMonitor path `/metrics/`
  (trailing slash, no redirect-follow) are both required for in-cluster Prometheus scraping.
- **Ollama embeddings** — the StatefulSet has 3 separate RWO PVCs, so an embedding model must be
  pulled on **every** replica or requests routed to a replica that lacks it fail. The model
  reconciler (see "Ollama model provisioning") handles this for anything declared in
  `app/resources/`; an ad-hoc `ollama pull` still has to be repeated per replica.
- **ConfigMap reloads** — the `litellm` controller is annotated `reloader.stakater.com/auto`, so
  Stakater Reloader restarts it automatically when the configmap changes.
