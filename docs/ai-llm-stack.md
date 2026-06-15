# AI / LLM stack

Self-hosted LLM stack in the `ai` namespace, fronted by **LiteLLM**. Patterns adapted from
[joryirving/home-ops](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm),
re-targeted to this cluster — **NVIDIA L4 GPUs + Ollama** instead of his AMD/Mac/llama.cpp gear.

## Components

| App          | Role                                                               | Status            |
| ------------ | ------------------------------------------------------------------ | ----------------- |
| `litellm`    | OpenAI-compatible gateway: routing, fallbacks, cache, metrics, MCP | live              |
| `ollama`     | local inference (3× L4, one per node); embeddings + general chat   | live              |
| `open-webui` | chat UI                                                            | live              |
| `toolhive`   | MCP servers (kubectl/flux/talos/searxng) wired into LiteLLM        | live              |
| `memini`     | agent long-term memory (sqlite + CPU embed/rerank)                 | live              |
| `llmkube`    | llama.cpp serving for select hot models on L4 (CUDA)               | Layer 4 (planned) |

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

## Rollout (staged)

1. **LiteLLM uplift** — model-groups + router fallbacks + commented hooks for MCP / embeddings /
   cloud providers.
2. **ToolHive + MCP** — operator + curated MCP servers (kubectl, flux, talos, searxng) wired into
   LiteLLM `mcp_servers`. (VirtualMCPServer aggregate + semantic filter deferred — see below.)
3. **memini** — agent memory; embeddings + rerank via tiny CPU llama.cpp servers, consolidation via
   LiteLLM.
4. **llmkube** — llama.cpp `Model` + `InferenceService` CRs on L4 (CUDA); registered as
   `self-hosted` group backends. Optional ComfyUI image gen.

Each layer is a separate commit on one branch (one PR). Cross-layer wiring (e.g. `mcp_servers`) is
staged as commented blocks in `litellm/app/configmap.yaml` and switched on when the producing layer
lands.

> Not ported from Jory's repo: `hermes`, `openclaw` (agent runtimes), and `agentmemory` (whose main
> consumers are hermes/openclaw).

## How to extend LiteLLM

- **Add a backend to a group** — add a `model_list` entry with an existing `model_name` and the
  next `order:`. LiteLLM balances / fails over within the group.
- **Add a cloud provider** — add the key to the `litellm` 1Password item, add a line to
  `externalsecret.yaml`'s `target.template.data`, then uncomment the matching stub in
  `configmap.yaml`. Don't reference an `os.environ/KEY` that isn't in the secret — the pod env read
  fails at startup.
- **Fallbacks** — `router_settings.fallbacks` is a list of `{model_name: [fallback, …]}`.

## MCP tools (ToolHive)

Layer 2 runs the [StackLok ToolHive](https://github.com/stacklok/toolhive) operator (`0.29.3`,
separate CRDs + operator charts) in `ai`, an `MCPGroup` (`mcp-tools`), and these MCP servers, all
wired into LiteLLM's `mcp_servers`:

| Server    | Source                          | Access                              |
| --------- | ------------------------------- | ----------------------------------- |
| `kubectl` | kubectl-mcp-server              | cluster read-only, secrets excluded |
| `flux`    | flux-operator-mcp               | Flux read-only (write is opt-in)    |
| `talos`   | talos-mcp                       | Talos `os:reader` (talosconfig SA)  |
| `searxng` | mcp-searxng → `searxng.default` | web search                          |

kubectl + flux share one read-only `ClusterRole` (`kubectl-mcp-readonly`) built from this cluster's
API groups with core `secrets` omitted — keep it in sync with `kubectl api-resources` as you add
CRDs. The talos MCP mounts a `talos.dev` `ServiceAccount`-minted `os:reader` talosconfig.

Deferred (add later): the `VirtualMCPServer` aggregate + `EmbeddingServer` (a single
`mcp.<domain>` endpoint for non-LiteLLM clients — it's what pulls in a Dragonfly + embedder),
`github` (needs a PAT in 1Password), `grafana-mcp` (needs a grafana MCP server), and LiteLLM's
`mcp_semantic_tool_filter` (only worth enabling past ~50 tools).

### Enabling flux-mcp write access

The flux MCP is read-only by default. To let it — and therefore any model behind LiteLLM —
reconcile / suspend / resume / apply / delete Flux objects, append to
`toolhive/mcp-servers/flux/rbac.yaml` and add it to that dir's `kustomization.yaml`:

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
endpoint to LiteLLM's `mcp_servers`. The service is `mcp-<name>` on the spec's `mcpPort`.

## Agent memory (memini)

Layer 3 runs [memini](https://github.com/eleboucher/memini) (sqlite backend) for agent long-term
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

## Gotchas

- **Public repo** — no LAN IPs / internal hostnames in git (see `CLAUDE.md`). Cluster service DNS
  and `${SECRET_*}` placeholders are fine.
- **Metrics** — `require_auth_for_metrics_endpoint: false` **and** ServiceMonitor path `/metrics/`
  (trailing slash, no redirect-follow) are both required for in-cluster Prometheus scraping.
- **Ollama embeddings** — the StatefulSet has 3 separate RWO PVCs, so an embedding model must be
  pulled on **every** replica or requests routed to a replica that lacks it fail.
- **ConfigMap reloads** — the `litellm` controller is annotated `reloader.stakater.com/auto`, so
  Stakater Reloader restarts it automatically when the configmap changes.
