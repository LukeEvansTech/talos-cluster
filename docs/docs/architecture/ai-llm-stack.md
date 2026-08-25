# AI / LLM stack

Self-hosted LLM stack in the `ai` namespace, fronted by **LiteLLM**. Patterns adapted from
[joryirving/home-ops](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm),
re-targeted to this cluster: **NVIDIA L4 GPUs + llama.cpp (llmkube)**.

## Components

| App          | Role                                                               | Status |
| ------------ | ------------------------------------------------------------------ | ------ |
| `litellm`    | OpenAI-compatible gateway: routing, fallbacks, cache, metrics, MCP | live   |
| `llmkube`    | llama.cpp model-serving operator (CUDA); 1 model active            | live   |
| `open-webui` | chat UI                                                            | live   |
| `toolhive`   | MCP servers (8 read-only servers) wired into LiteLLM               | live   |
| `memini`     | agent long-term memory (SQLite + CPU embed/rerank)                 | live   |
| `hermes`     | NousResearch hermes-agent gateway + dashboard (memini-backed)      | live   |
| `hermeswebui`| chat web frontend for hermes (via its API server)                  | live   |

LiteLLM persists to CNPG `postgres18` (`litellm` db) and caches in Dragonfly. Internal-only route
(`litellm.${SECRET_DOMAIN}` on envoy-internal).

## Model serving (llmkube)

[llmkube](https://github.com/defilantech/LLMKube) is the sole GPU inference tier. Each model is
declared as a `Model` CR (weights source + hardware) plus an `InferenceService` CR (the serving
pod), one file per model under `kubernetes/apps/ai/llmkube/models/`.

**Active model** — one GPU model serves both LiteLLM groups:

| LiteLLM model name        | InferenceService  | Notes                                    |
| ------------------------- | ----------------- | ---------------------------------------- |
| `self-hosted`             | `llama-nvidia`    | Default; vision-capable via mmproj       |
| `self-hosted-uncensored`  | `llama-nvidia`    | Alias, same backend; no cloud fallback   |

The model is **Qwen3.8-27B Heretic-abliterated** (0bserverx RVN Q4_K_S, MTP head retained):
picked as the closest-to-vanilla uncensored build (KL ~0.0085 vs base, refusals 0–1/100,
official chat template). Its hybrid Gated-DeltaNet layout keeps KV at ~32KB/token (q8_0), so
one 24GB L4 serves 128k of context (2 slots × 64k per request) alongside the weights and the
vision projector — no YaRN, no `--override-kv`.

Weight files are declared as `hf://` URIs pointing to single-file public GGUFs on Hugging Face.
llmkube downloads and caches them on the shared CephFS RWX `modelCache` PVC (`ceph-filesystem`
storage class), so a cold start auto-heals without manual staging.

Anti-affinity (`podAntiAffinity`) keeps one resident model per L4: the cluster has 3 cards and
runs 1 model, preserving two cards for other GPU workloads (Plex/Jellyfin transcodes, Whisper).
No model swapping occurs during normal operation. The `gpu-preemptible` PriorityClass is set on
all llmkube pods so higher-priority workloads can evict them if needed.

The model is vision-enabled: the `InferenceService` mounts the repo's `mmproj-*-Q8_0.gguf`
multimodal projector alongside the main GGUF. This is the model that `loupe` (image analysis)
consumes via LiteLLM.

To add a model: drop a `Model` + `InferenceService` manifest under `llmkube/models/`, add a
`model_list` entry to `litellm/app/configmap.yaml`, and commit. Flux reconciles both.

### Model groups

LiteLLM `model_name` groups make the serving tier transparent to clients:

- **`self-hosted`**: `llama-nvidia` (order 1), with a live cloud fallback to `openrouter/auto`
  via `router_settings.fallbacks` (the OpenRouter key is in the `litellm` ExternalSecret).
- **`self-hosted-uncensored`**: the same `llama-nvidia` backend under a second name. Kept as
  its own group because its fallback semantics differ: no cloud fallback by design (a cloud
  model would reintroduce refusals), and existing clients keep working unchanged.

## In-cluster consumers

Three in-cluster apps route through LiteLLM using the standardized OpenAI env contract:

| App             | Namespace | LiteLLM model    |
| --------------- | --------- | ---------------- |
| `contracthound` | `default` | `self-hosted`    |
| `subspy`        | `default` | `self-hosted`    |
| `loupe`         | `custom`  | `self-hosted`    |

All three consume:

- `LLM_PROVIDER=openai`
- `OPENAI_API_BASE_URL=http://litellm.ai.svc.cluster.local:4000/v1`
- `OPENAI_MODEL=self-hosted`
- `OPENAI_API_KEY` from the `litellm` 1Password item via ExternalSecret

## Rollout (staged)

1. **LiteLLM uplift**: model-groups + router fallbacks + commented hooks for MCP / embeddings /
   cloud providers.
2. **ToolHive + MCP**: operator + curated MCP servers (kubectl, flux, talos, searxng) wired into
   LiteLLM `mcp_servers`.
3. **memini**: agent memory; embeddings + rerank via tiny CPU llama.cpp servers, consolidation via
   LiteLLM.
4. **llmkube**: operator + CephFS modelCache; 2 active models at the time (`self-hosted`,
   `self-hosted-uncensored` — since consolidated onto one backend, see "Active model" above).
5. **Ollama decommission**: Ollama removed; contracthound/subspy/loupe repointed to LiteLLM.

Each layer is a separate commit on one branch (one PR). `mcp_servers` and
`mcp_semantic_tool_filter` are now fully active in `litellm/app/configmap.yaml`, and one cloud
provider is live: `openrouter/auto` is an active `model_list` entry serving as the `self-hosted`
group's fallback. The remaining cloud-provider stubs are still commented out.

> Since ported from Jory's repository: `hermes` (plus a `hermeswebui` chat frontend). Still not
> ported: `openclaw` (agent runtime) and `agentmemory` (memini covers agent memory here).

## How to extend LiteLLM

- **Add a backend to a group**: add a `model_list` entry with an existing `model_name` and the
  next `order:`. LiteLLM balances / fails over within the group.
- **Add a cloud provider**: add the key to the `litellm` 1Password item, add a line to
  `externalsecret.yaml`'s `target.template.data`, then uncomment the matching stub in
  `configmap.yaml`. Don't reference an `os.environ/KEY` that isn't in the secret. The pod env read
  fails at startup.
- **Fallbacks**: `router_settings.fallbacks` is a list of `{model_name: [fallback, …]}`.

## Consuming the stack from a workstation (opencode)

Any OpenAI-compatible client can drive the self-hosted models through the gateway: internal route
`https://litellm.${SECRET_DOMAIN}/v1`, models `self-hosted` and `self-hosted-uncensored`.
[opencode](https://opencode.ai) is wired this way as a custom provider:

- **Provider**: `@ai-sdk/openai-compatible`, `baseURL: https://litellm.${SECRET_DOMAIN}/v1`.
  Put the LiteLLM key in the client's own credential store (opencode: `opencode auth login` →
  `~/.local/share/opencode/auth.json`), **never** as a literal in a shared or committed config.
- **MCP tools**: point a remote MCP server at LiteLLM's MCP gateway,
  `https://litellm.${SECRET_DOMAIN}/mcp/` (the `litellm-mcp-server`), with the LiteLLM key
  as `Authorization: Bearer …`. Curate the server set with the `x-mcp-servers` header (e.g.
  `kubectl,flux,talos,searxng`). Requesting **all** servers times out, and the full tool list
  bloats every request (heavy on the small-context local models, so prefer a frontier model for
  tool-heavy work).
- **Gotchas**: the self-hosted model is a Qwen *thinking* model (send `think: false` /
  `enable_thinking: false`, or lower `reasoning_effort`, for non-reasoning output — Qwen3.8
  defaults to heavy reasoning). Both group names hit the same backend, so there is no
  model-swap cost for switching between them. See the context-budget note below for opencode's
  window requirements.

Prefer a **scoped LiteLLM virtual key** (`/key/generate`, limited to the `self-hosted*` models) over
the master key for any workstation client: it's revocable on its own.

### opencode and the context budget

opencode's agent sends **~41k tokens before any user input**: its system prompt plus built-in tool
schemas (measured; LiteLLM is not injecting MCP tools, a plain request is ~18 tokens and one with a
tool is ~130). Qwen3.8-27B's 262k native context makes this a non-issue: both groups serve **64k
per request** (`contextSize 131072 ÷ 2 parallelSlots` in `llmkube/models/qwen3.8-27b.yaml`), no
YaRN, no `--override-kv`. Either group hosts opencode; use `self-hosted-uncensored` if a cloud
fallback mid-session would be unwelcome.

The YaRN + `--override-kv` workaround the previous Qwen3-30B-A3B uncensored model needed (the
pinned server hard-capped slots to the trained context even with correct YaRN args,
[llama.cpp#22140](https://github.com/ggml-org/llama.cpp/issues/22140)) is retired with it. The
verification habit it taught still stands: a `Ready` phase and a clean `kustomize build` do
**not** prove the served window — confirm with `/props`
(`.default_generation_settings.n_ctx == 65536`, the per-slot share of `contextSize 131072`)
after any context change.

## MCP tools (ToolHive)

Layer 2 runs the [StackLok ToolHive](https://github.com/stacklok/toolhive) operator (separate
CRDs + operator charts, see `toolhive/app/ocirepository.yaml` for the current pin) in `ai`, an
`MCPGroup` (`mcp-tools`), and these MCP servers, all wired into LiteLLM's `mcp_servers`:

| Server    | Source                          | Access                                                  |
| --------- | ------------------------------- | ------------------------------------------------------- |
| `kubectl` | kubectl-mcp-server              | cluster read-only, secrets excluded                     |
| `flux`    | flux-operator-mcp               | Flux read-only (write is opt-in)                        |
| `talos`   | talos-mcp                       | Talos `os:reader` (talosconfig SA)                      |
| `searxng` | mcp-searxng → `searxng.default` | web search                                              |
| `github`  | github-mcp-server               | GitHub read-only (`GITHUB_READ_ONLY`, fine-grained PAT) |
| `grafana` | grafana/mcp-grafana             | Grafana Viewer SA token (read-only)                     |
| `arr`     | mcp-arr                         | Sonarr / Radarr / Prowlarr tools (per-app API keys)     |
| `seerr`   | overseerr-mcp                   | Overseerr request + discovery tools                     |

kubectl + flux share one read-only `ClusterRole` (`kubectl-mcp-readonly`) built from this cluster's
API groups with core `secrets` omitted. Keep it in sync with `kubectl api-resources` as you add
CRDs. The talos MCP mounts a `talos.dev` `ServiceAccount`-minted `os:reader` talosconfig.

The `mcp_semantic_tool_filter` is **on** (top_k 8, embeddings via the `all-minilm` model on the
CPU `llama-embed` pod): with 8 servers' worth of tools it trims each request to the most
relevant ones. `github` + `grafana` are read-only, via a fine-grained PAT (`toolhive-github`) and a
Grafana Viewer service-account token (`toolhive-grafana`).

Deferred (add later): the `VirtualMCPServer` aggregate + `EmbeddingServer` (a single
`mcp.<domain>` endpoint for non-LiteLLM clients, which is what pulls in a Dragonfly + embedder).

### Enabling flux-mcp write access

The flux MCP is read-only by default. To let it (and therefore any model behind LiteLLM)
reconcile / suspend / resume / apply / delete Flux objects, append to
`toolhive/mcp-servers/flux/rbac.yaml` (no `kustomization.yaml` change needed, `rbac.yaml` is
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

This grants an LLM mutate access to the cluster's GitOps controller. Enable only if you trust the
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

- `llama-embed`: all-MiniLM-L6-v2 (384-dim), `--embeddings`, OpenAI `/v1`.
- `llama-rerank`: Qwen3-Reranker-0.6B, `--rerank`.

Both run on **CPU** (`ghcr.io/ggml-org/llama.cpp:server`, GPU/Vulkan bits stripped). The L4s are
spoken for by llmkube, and these models are small (~30 MB / ~600 MB). memini's consolidation LLM is
LiteLLM's `self-hosted` group.

Secrets: a generated `MEMINI_API_KEY` (Talos vault item `memini`) + `LITELLM_MASTER_KEY` (reused
from the `litellm` item). Data PVC via the volsync component (10Gi). Route:
`memini.${SECRET_DOMAIN}` (envoy-internal).

To move embeddings onto the GPU later, swap `llama-embed`/`llama-rerank` for llmkube
`InferenceService`s and repoint `MEMINI_EMBED_BASE_URL` / `MEMINI_RERANK`.

## Gotchas

- **Public repository**: no LAN IPs / internal hostnames in Git (see `CLAUDE.md`). Cluster service DNS
  and `${SECRET_*}` placeholders are fine.
- **Metrics**: `require_auth_for_metrics_endpoint: false` **and** ServiceMonitor path `/metrics/`
  (trailing slash, no redirect-follow) are both required for in-cluster Prometheus scraping.
- **CephFS dependency**: llmkube's shared `modelCache` PVC requires `ceph-filesystem` (RWX).
  Without it, multi-replica `InferenceService` pods fail to schedule (only one pod can hold an RWO
  volume at a time). The `ceph-filesystem` storage class is provisioned by Rook-Ceph.
- **ConfigMap reloads**: the `litellm` controller is annotated `reloader.stakater.com/auto`, so
  Stakater Reloader restarts it automatically when the configmap changes.
- **Cross-namespace netpol**: `kubernetes/apps/ai/netpol.yaml` allows ingress to the `ai`
  namespace from the `network` namespace (gateway), plus a second `CiliumNetworkPolicy`
  (`allow-litellm-from-consumers`) that grants the `default` and `custom` namespaces ingress to the
  `litellm` endpoint specifically. A new consumer namespace needs adding to that policy's
  `fromEndpoints` list before its calls to `litellm.ai.svc.cluster.local:4000` will connect.
- **`drop_params` is deliberately off**: with it enabled, LiteLLM silently strips
  `reasoning_effort` before a request reaches the llama.cpp backend (treated as unsupported for
  the `openai/` provider entry), which defeats disabling Qwen3.8's thinking mode (verified
  2026-08-25). If a real `UnsupportedParamsError` 400 ever shows up, scope the drop with a
  per-model `additional_drop_params` on that model entry instead of re-enabling this globally.
