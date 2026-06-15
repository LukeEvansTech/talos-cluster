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
| `toolhive`   | MCP server fleet, aggregated + proxied to LiteLLM                  | Layer 2 (planned) |
| `memini`     | agent long-term memory                                             | Layer 3 (planned) |
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
2. **ToolHive + MCP** — operator + curated MCP servers (kubectl, flux, talos, github, grafana,
   searxng), aggregated via `VirtualMCPServer`; wired into LiteLLM `mcp_servers` + semantic filter.
3. **memini** — agent memory; embeddings via Ollama, consolidation via LiteLLM.
4. **llmkube** — llama.cpp `Model` + `InferenceService` CRs on L4 (CUDA); registered as
   `self-hosted` group backends. Optional ComfyUI image gen.

Each layer is its own PR. Cross-layer wiring (e.g. `mcp_servers`) is staged as commented blocks in
`litellm/app/configmap.yaml` and switched on when the producing layer lands.

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

## Gotchas

- **Public repo** — no LAN IPs / internal hostnames in git (see `CLAUDE.md`). Cluster service DNS
  and `${SECRET_*}` placeholders are fine.
- **Metrics** — `require_auth_for_metrics_endpoint: false` **and** ServiceMonitor path `/metrics/`
  (trailing slash, no redirect-follow) are both required for in-cluster Prometheus scraping.
- **Ollama embeddings** — the StatefulSet has 3 separate RWO PVCs, so an embedding model must be
  pulled on **every** replica or requests routed to a replica that lacks it fail.
- **ConfigMap reloads** — the `litellm` controller is annotated `reloader.stakater.com/auto`, so
  Stakater Reloader restarts it automatically when the configmap changes.
