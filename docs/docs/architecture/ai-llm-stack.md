# AI / LLM stack

Self-hosted LLM stack in the `ai` namespace, fronted by **LiteLLM**. Patterns adapted from
[joryirving/home-ops](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm),
re-targeted to this cluster: **NVIDIA L4 GPUs + llama.cpp (llmkube)**.

## Components

| App           | Role                                                                               | Status |
| ------------- | ---------------------------------------------------------------------------------- | ------ |
| `litellm`     | OpenAI-compatible gateway: routing, fallbacks, cache, metrics, MCP                 | live   |
| `llmkube`     | llama.cpp model-serving operator (CUDA); 1 model active                            | live   |
| `open-webui`  | chat UI (SearXNG web search, Dragonfly websockets)                                 | live   |
| `toolhive`    | MCP servers (9 read-only) + a VirtualMCPServer gateway                             | live   |
| `memini`      | agent long-term memory (SQLite + CPU embed/rerank)                                 | live   |
| `hermes`      | NousResearch hermes-agent gateway + dashboard (memini-backed, ToolHive VMCP-wired) | live   |
| `hermeswebui` | chat web frontend for hermes (via its API server)                                  | live   |
| `repowiki`    | AI-generated per-repository wiki (mkdocs-material + CronJob)                       | live   |
| `foreman`     | LLMKube coder/gate/reviewer control plane — trialled, manifests in `.archive/`     | archived |

LiteLLM persists to CNPG `postgres18` (`litellm` db) and caches in Dragonfly. Internal-only route
(`litellm.${SECRET_DOMAIN}` on envoy-internal).

## Model serving (llmkube)

[llmkube](https://github.com/defilantech/LLMKube) is the sole GPU inference tier. Each model is
declared as a `Model` CR (weights source + hardware) plus an `InferenceService` CR (the serving
pod), one file per model under `kubernetes/apps/ai/llmkube/models/`.

**Active model** — one GPU model serves both LiteLLM groups:

| LiteLLM model name       | InferenceService | Notes                                  |
| ------------------------ | ---------------- | -------------------------------------- |
| `self-hosted`            | `llama-nvidia`   | Default; vision-capable via mmproj     |
| `self-hosted-uncensored` | `llama-nvidia`   | Alias, same backend; no cloud fallback |

The model is **Qwen3.8-27B Heretic-abliterated** (0bserverx RVN Q4_K_S, MTP head retained):
picked as the closest-to-vanilla uncensored build (KL ~0.0085 vs base, refusals 0–1/100,
official chat template). Its hybrid Gated-DeltaNet layout keeps KV at ~32KB/token (q8_0), so
one 24GB L4 serves 128k of context across 2 slots alongside the weights and the vision
projector — no YaRN, no `--override-kv`. `--kv-unified` (#4579) pools that context rather than
splitting it statically, so one request may use the whole 128k window while the other slot is
idle, instead of a fixed 64k-per-slot cap.

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
`LiteLLMModel` CR under `litellm/app/models/`, and commit. Flux reconciles both.

### Model groups

LiteLLM `model_name` groups make the serving tier transparent to clients:

- **`self-hosted`**: `llama-nvidia` (order 1), with a live cloud fallback to `openrouter/auto`
  via `router_settings.fallbacks` (the OpenRouter key is in the `litellm` ExternalSecret).
- **`self-hosted-uncensored`**: the same `llama-nvidia` backend under a second name. Kept as
  its own group because its fallback semantics differ: no cloud fallback by design (a cloud
  model would reintroduce refusals), and existing clients keep working unchanged.

## In-cluster consumers

Five in-cluster apps route through LiteLLM, all at `http://litellm.ai.svc.cluster.local:4000/v1`
and the `self-hosted` model (with the `openrouter/auto` cloud fallback in scope):

| App             | Namespace | Key env var         |
| --------------- | --------- | ------------------- |
| `contracthound` | `default` | `OPENAI_API_KEY`    |
| `subspy`        | `default` | `OPENAI_API_KEY`    |
| `jobops`        | `default` | `LLM_API_KEY`       |
| `loupe`         | `custom`  | `OPENAI_API_KEY`    |
| `todoist-sort`  | `custom`  | `ANTHROPIC_API_KEY` |

All five live outside the `ai` namespace, so none of them mounts an `ai` Secret directly. Each
gets an operator-issued **scoped** key instead of the account-wide master key: a
`LiteLLMVirtualKey` CR in `litellm/app/virtualkeys/<app>.yaml` (`ai` namespace, scoped to
`self-hosted` + `openrouter/auto`) mints the key and writes it to an in-namespace Secret; a
paired `PushSecret` then writes that key back to the `litellm` 1Password item as property
`LITELLM_<APP>_API_KEY` (`updatePolicy: Replace`, `refreshInterval: 1h`). The consumer's own
ExternalSecret extracts that property like any other `litellm`-item field — no cross-namespace
Secret access required. See the consumer's own `externalsecret.yaml` for the exact template line.

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
`mcp_semantic_tool_filter` are now fully active (rendered by litellm-operator from the
`LiteLLMMCPServer` CRs and `litellmSettings` in `litellm/app/litellmproxy.yaml`), and one cloud
provider is live: `openrouter/auto` is an active `LiteLLMModel` serving as the `self-hosted`
group's fallback. The remaining cloud-provider stubs are commented reference in
`litellm/app/models/kustomization.yaml`.

> Since ported from Jory's repository: `hermes` (plus a `hermeswebui` chat frontend). Still not
> ported: `openclaw` (agent runtime) and `agentmemory` (memini covers agent memory here).

## How to extend LiteLLM

The gateway is `litellm-operator`-managed: a `LiteLLMProxy` CR (`litellm/app/litellmproxy.yaml`,
`applyMode: file`) plus `LiteLLMModel` and `LiteLLMMCPServer` CRs the operator adopts via
`proxyRef: litellm` and renders into `config.yaml`, rolling the Deployment on change. There is no
`litellm/app/configmap.yaml` any more — `store_model_in_db: false` in the proxy's
`generalSettings`, so the rendered CRs (not the Postgres-backed admin UI) are the model source of
truth.

- **Add a backend to a group**: add a `LiteLLMModel` CR under `litellm/app/models/` with an
  existing `spec.modelName` and the next `params.additional.order`. LiteLLM balances / fails over
  within the group.
- **Add a cloud provider**: add the key to the `litellm` 1Password item, add a line to
  `externalsecret.yaml`'s `target.template.data`, then add a `LiteLLMModel` CR under
  `litellm/app/models/` (commented examples for Jory's set live in that directory's
  `kustomization.yaml`). Don't reference an `os.environ/KEY` that isn't in the secret. The pod env
  read fails at startup.
- **Fallbacks**: `litellmproxy.yaml`'s `routerSettings.fallbacks` is a list of
  `{model_name: [fallback, …]}`.
- **A consumer's scoped key (same namespace)**: add a `LiteLLMVirtualKey` CR in the consumer's own
  app directory (`proxyRef: litellm`, `secretName: litellm-key-<app>`, `secretKey: api-key`)
  rather than handing out the master key. The operator creates the key via the proxy's admin API
  and writes it to that Secret; consume it with a `secretKeyRef`. See
  `memini/app/virtualkey.yaml`.
- **A consumer's scoped key (cross-namespace)**: a consumer outside `ai` can't mount an `ai`
  Secret directly. Add the `LiteLLMVirtualKey` CR (`ai` namespace) plus a paired `PushSecret`
  under `litellm/app/virtualkeys/<app>.yaml` instead — the `PushSecret` writes the operator-minted
  key back to the `litellm` 1Password item as `LITELLM_<APP>_API_KEY`, and the consumer's existing
  ExternalSecret extracts it like any other field on that item. See "In-cluster consumers" above.

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
- **Gotchas**: the self-hosted model is a Qwen _thinking_ model (send `think: false` /
  `enable_thinking: false`, or lower `reasoning_effort`, for non-reasoning output — Qwen3.8
  defaults to heavy reasoning). Both group names hit the same backend, so there is no
  model-swap cost for switching between them. See the context-budget note below for opencode's
  window requirements.

Prefer a **scoped LiteLLM virtual key** (`/key/generate`, limited to the `self-hosted*` models) over
the master key for any workstation client: it's revocable on its own.

### opencode and the context budget

opencode's agent sends **~41k tokens before any user input**: its system prompt plus built-in tool
schemas (measured; LiteLLM is not injecting MCP tools, a plain request is ~18 tokens and one with a
tool is ~130). Qwen3.8-27B's 262k native context makes this a non-issue: `--kv-unified` (#4579)
lets **one request use the full 131072-token window** (`contextSize` in
`llmkube/models/qwen3.8-27b.yaml`) while the other of the 2 `parallelSlots` sits idle, rather than
splitting it into a static 64k-per-slot cap. Either group hosts opencode; use
`self-hosted-uncensored` if a cloud fallback mid-session would be unwelcome.

The YaRN + `--override-kv` workaround the previous Qwen3-30B-A3B uncensored model needed (the
pinned server hard-capped slots to the trained context even with correct YaRN args,
[llama.cpp#22140](https://github.com/ggml-org/llama.cpp/issues/22140)) is retired with it. The
verification habit it taught still stands: a `Ready` phase and a clean `kustomize build` do
**not** prove the served window — confirm with `/props`
(`.default_generation_settings.n_ctx == 131072`, the full unified `contextSize`, not a per-slot
share) after any context change.

## MCP tools (ToolHive)

Layer 2 runs the [StackLok ToolHive](https://github.com/stacklok/toolhive) operator (separate
CRDs + operator charts, see `toolhive/app/ocirepository.yaml` for the current pin) in `ai`, an
`MCPGroup` (`mcp-tools`), and these MCP servers, all wired into LiteLLM's `mcp_servers`:

| Server    | Source                          | Access                                                    |
| --------- | ------------------------------- | --------------------------------------------------------- |
| `kubectl` | kubectl-mcp-server              | cluster read-only, secrets excluded                       |
| `flux`    | flux-operator-mcp               | Flux read + write (reconcile/suspend/resume/apply/delete) |
| `talos`   | talos-mcp                       | Talos `os:reader` (talosconfig SA)                        |
| `searxng` | mcp-searxng → `searxng.default` | web search                                                |
| `github`  | github-mcp-server               | GitHub read-only (`GITHUB_READ_ONLY`, fine-grained PAT)   |
| `grafana` | grafana/mcp-grafana             | Grafana Viewer SA token (read-only)                       |
| `arr`     | mcp-arr                         | Sonarr / Radarr / Prowlarr tools (per-app API keys)       |
| `seerr`   | overseerr-mcp                   | Overseerr request + discovery tools                       |
| `hamcp`   | ha-mcp                          | Home Assistant tools (scoped long-lived token)            |

kubectl + flux share one read-only `ClusterRole` (`kubectl-mcp-readonly`) built from this cluster's
API groups with core `secrets` omitted. Keep it in sync with `kubectl api-resources` as you add
CRDs. The talos MCP mounts a `talos.dev` `ServiceAccount`-minted `os:reader` talosconfig.

The `mcp_semantic_tool_filter` is **on** (top_k 8, embeddings via the `all-minilm` model on the
CPU `llama-embed` pod): with 9 servers' worth of tools it trims each request to the most
relevant ones. `github` + `grafana` are read-only, via a fine-grained PAT (`toolhive-github`) and a
Grafana Viewer service-account token (`toolhive-grafana`).

### MCP gateway (VirtualMCPServer)

A `VirtualMCPServer` named `mcp-gateway` (`toolhive/gateway/virtualmcpserver.yaml`) aggregates
every backend in the `mcp-tools` `MCPGroup` behind one endpoint, for hermes, workstation opencode,
and any other client that talks MCP directly instead of going through LiteLLM's `mcp_servers`
wiring. Tool names are namespaced `{workload}_<tool>` (e.g. `searxng_search`) to resolve
collisions across backends. Because 9 backends' worth of raw tool definitions is too large for
most client context windows, the built-in optimizer exposes only `find_tool`/`call_tool` to
clients and resolves the right backend tool semantically. It embeds via `qwen3-embedding`
(Qwen3-Embedding-0.6B, 8k context; `toolhive/gateway/embed/`), a dedicated CPU llama.cpp server,
rather than LiteLLM's `all-minilm` (all-MiniLM-L6-v2): MiniLM's BERT architecture has a hard
512-token limit and some kubectl tool descriptions exceed it, which used to terminate every VMCP
session with an OpenAI 400 "input larger than max context size" error. `all-minilm` stays in
place for memini and LiteLLM's own `mcp_semantic_tool_filter` above, neither of which hits that
limit. Sessions are stored in Dragonfly so the Deployment can scale beyond one replica.

- **External URL**: `https://mcp.${SECRET_DOMAIN}/mcp`, header `x-api-key: <key>`. Keys live as
  fields on the `toolhive` 1Password item; the workstation's key is
  `TOOLHIVE_WORKSTATION_API_KEY`. Add a client by adding a field to that item plus a matching
  template key in `toolhive/gateway/externalsecret.yaml`'s `mcp-gateway-api-keys` ExternalSecret —
  any key's value in the resulting Secret is accepted (Envoy Gateway's `SecurityPolicy` doesn't
  distinguish which one matched).
- **In-cluster URL**: `http://vmcp-mcp-gateway.ai.svc.cluster.local:4483/mcp`, no auth (anonymous
  `incomingAuth` — the API-key gate lives at the Envoy Gateway edge, not in the vmcp app itself).
- Metrics are scraped from the same port via the existing `prometheus` `MCPTelemetryConfig`; a
  Grafana dashboard is imported from ToolHive's upstream OTEL-scrape dashboard JSON.

### flux-mcp write access (enabled)

The flux MCP has had write access to Flux CRDs since this was enabled: `flux-mcp-write`, a
`ClusterRole` + `ClusterRoleBinding` in `toolhive/mcp-servers/flux/rbac.yaml`, grants the
`flux-mcp` ServiceAccount `create`/`patch`/`update`/`delete` on every Flux kind installed on this
cluster — one rule per apiGroup (`fluxcd.controlplane.io`, `helm.toolkit.fluxcd.io`,
`kustomize.toolkit.fluxcd.io`, `notification.toolkit.fluxcd.io`, `source.toolkit.fluxcd.io`),
each listing its resources by name rather than `resources: ["*"]` — a wildcard trips Trivy
KSV-0046 and Checkov's wildcard-RBAC check even when the apiGroups are this narrow. Nothing
outside Flux CRDs, and no `secrets` access (core `""` is never included). `image.toolkit.fluxcd.io`
has no rule because this cluster doesn't run the Flux image-automation controller — add one
(`imagepolicies`, `imagerepositories`, `imageupdateautomations`) if that ever changes, and add
any other new Flux kind by hand when a component upgrade introduces one. This lets it (and
therefore any model behind LiteLLM) reconcile, suspend, resume, apply, and delete Flux objects.

The `flux-operator-mcp` image's own `--read-only` flag defaults to `false` and this deployment
has never set it, so the write tools were already registered at the MCP layer before this
change — RBAC was, and remains, the only enforcement boundary.

This grants an LLM mutate access to the cluster's GitOps controller; the owner has accepted
that blast radius. **To revoke**, delete the `flux-mcp-write` `ClusterRole` and
`ClusterRoleBinding` from `toolhive/mcp-servers/flux/rbac.yaml` (the two read-only bindings are
unaffected).

### Adding an MCP server

Drop an `MCPServer` (operator-managed) or `MCPServerEntry` (remote URL) with `groupRef: mcp-tools`
under `toolhive/mcp-servers/<name>/`, list it in that dir's `kustomization.yaml`, then add its
endpoint to LiteLLM's `mcp_servers`. The service name depends on the transport:

- **Native `streamable-http` transport** (e.g. kubectl, flux): ToolHive creates `mcp-<name>` on the
  spec's `mcpPort`.
- **`stdio` transport with `proxyMode: streamable-http`** (e.g. github, grafana): ToolHive creates
  `mcp-<name>-proxy` on the spec's `proxyPort` (typically 8080).

#### What clients actually see

The gateway runs ToolHive's **semantic optimizer**, so a client that lists tools gets exactly two
meta-tools, not the ~540 aggregated ones: `find_tool(tool_description)` returns the backend tools
that best match a natural-language need (embedded via `qwen3-embedding`), and `call_tool` invokes
one by its `<backend>_<tool>` name. Point agents at those two; do not expect `kubectl_*` names in
`tools/list`.

Warm-up: the first session after the gateway or embedder pod restarts builds the embedding store
(~540 descriptions through the CPU `toolhiveembed` server, ~1–2 min) and that first `initialize`
can drop with "connection closed"; the store persists, so every later session initializes in about
a second. If sessions keep failing, check `toolhiveembed` for `OOMKilled` (its RSS peaks ~3.2Gi
during the build; limit is 6Gi) and the VMCP logs for `exceed_context_size_error`.

## Agent memory (memini)

Layer 3 runs [memini](https://github.com/eleboucher/memini) (SQLite backend) for agent long-term
memory, plus two tiny CPU `llama.cpp` model servers in `ai`:

- `llama-embed`: all-MiniLM-L6-v2 (384-dim), `--embeddings`, OpenAI `/v1`.
- `llama-rerank`: Qwen3-Reranker-0.6B, `--rerank`.

Both run on **CPU** (`ghcr.io/ggml-org/llama.cpp:server`, GPU/Vulkan bits stripped). The L4s are
spoken for by llmkube, and these models are small (~30 MB / ~600 MB). memini's consolidation LLM is
LiteLLM's `self-hosted` group.

Secrets: a generated `MEMINI_API_KEY` (Talos vault item `memini`) + a scoped `LiteLLMVirtualKey`
(`litellm-key-memini`, see `memini/app/virtualkey.yaml`) rather than the master key. Data PVC via
the volsync component (10Gi). Route: `memini.${SECRET_DOMAIN}` (envoy-internal).

To move embeddings onto the GPU later, swap `llama-embed`/`llama-rerank` for llmkube
`InferenceService`s and repoint `MEMINI_EMBED_BASE_URL` / `MEMINI_RERANK`.

## repowiki

`repowiki` generates an AI-written wiki for every repository in the `LukeEvansTech` GitHub
account and serves it as a static site. Two controllers share one RWO `ceph-block` PVC:

- **`mkdocs`** (Deployment): `squidfunk/mkdocs-material` in `serve --dirty` mode, rendering
  whatever is committed to the git repository on the shared PVC. This is what the
  `repowiki.${SECRET_DOMAIN}` route (envoy-internal) serves.
- **`repowiki-gen`** (CronJob, every 12h): clones/updates each repository from
  `repos.txt`, has the `self-hosted` LiteLLM model plan a page set per repository, writes the
  pages, and commits them into the git repository on the PVC. A `podAffinity` pins it to the
  same node as the `mkdocs` pod, since the PVC is ReadWriteOnce.

Ported from Jory's [`repo-wiki`](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm/repo-wiki);
`generate.py` (in `kubernetes/apps/ai/repowiki/app/configmap.yaml`) is otherwise unmodified from
upstream — only the git commit identity and env values changed for this cluster.

**Adding or removing a repository**: edit `repos.txt` in `configmap.yaml` and commit — no
Helm/Kustomize change needed. The generator only regenerates a repository once its `HEAD` SHA
changes since the last successful run, so a new entry is picked up on the CronJob's next
occurrence.

**Pacing**: `MAX_REPOS_PER_RUN: "2"` caps each run to two repositories (whichever are most stale),
so a full pass over a large `repos.txt` takes several days, not one run. This is deliberate —
`MAX_PAGES_PER_REPO: "20"` pages per repository through an LLM planning + writing pass is not
cheap, and the CronJob shares the `self-hosted` LiteLLM model with every other consumer.
Increase `MAX_REPOS_PER_RUN` only if the model has headroom.

## Gotchas

- **Public repository**: no LAN IPs / internal hostnames in Git (see `CLAUDE.md`). Cluster service DNS
  and `${SECRET_*}` placeholders are fine.
- **Metrics**: `require_auth_for_metrics_endpoint: false` **and** ServiceMonitor path `/metrics/`
  (trailing slash, no redirect-follow) are both required for in-cluster Prometheus scraping.
- **CephFS dependency**: llmkube's shared `modelCache` PVC requires `ceph-filesystem` (RWX).
  Without it, multi-replica `InferenceService` pods fail to schedule (only one pod can hold an RWO
  volume at a time). The `ceph-filesystem` storage class is provisioned by Rook-Ceph.
- **Config changes roll the pod automatically**: litellm-operator hashes the rendered
  `config.yaml` into a `litellm.home-operations.com/config-hash` pod annotation, so editing a
  `LiteLLMModel`/`LiteLLMMCPServer`/`LiteLLMProxy` CR rolls the Deployment on its own — no
  Reloader annotation or manual restart needed.
- **open-webui's database is still SQLite**: on a `ReadWriteOnce` ceph-block PVC, with `strategy:
Recreate` and `TIMER_POLL_INTERVAL: "30"` working around a full-table-scan bug in the unused
  scheduler loop (see the HelmRelease comments). Web search, RAG embeddings, and Dragonfly-backed
  websockets are independent of this — Postgres migration is a separate, not-yet-scheduled
  decision.
- **Cross-namespace netpol**: `kubernetes/apps/ai/netpol.yaml` allows ingress to the `ai`
  namespace from the `network` namespace (gateway), plus a second `CiliumNetworkPolicy`
  (`allow-litellm-from-consumers`) that grants the `default` and `custom` namespaces ingress to the
  `litellm` endpoint specifically. A new consumer namespace needs adding to that policy's
  `fromEndpoints` list before its calls to `litellm.ai.svc.cluster.local:4000` will connect.
- **`drop_params` is deliberately off**: with it enabled, LiteLLM silently strips
  `reasoning_effort` before a request reaches the llama.cpp backend (treated as unsupported for
  the `openai/` provider entry), which defeats disabling Qwen3.8's thinking mode (verified
  2026-08-25). If a real `UnsupportedParamsError` 400 ever shows up, scope the drop with a
  `params.dropParams` on that model's `LiteLLMModel` CR instead of re-enabling this globally.
  Turning `drop_params` off alone did not forward the param either — LiteLLM's
  `UnsupportedParamsError` check runs independently of it, so the `self-hosted` and
  `self-hosted-uncensored` `LiteLLMModel` CRs also list `reasoning_effort` in
  `params.additional.allowed_openai_params` (rendered into `litellm_params.allowed_openai_params`),
  which is the actual forwarding mechanism.
