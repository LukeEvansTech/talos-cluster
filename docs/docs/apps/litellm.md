# LiteLLM

## Purpose

LiteLLM is an OpenAI-compatible LLM gateway in the `ai` namespace. It fronts the local llmkube
(llama.cpp) model-serving tier and cloud providers behind one API, so in-cluster consumers target a single endpoint and
get virtual keys, per-key budgets, request logging, model-group routing, fallbacks, and an admin UI.
See the [AI / LLM stack](../architecture/ai-llm-stack.md) page for how it fits the wider stack.

## Design decisions

- Deployed as a `LiteLLMProxy` CR (`litellm-operator`, `applyMode: file`) rather than a HelmRelease;
  the operator owns the Deployment, Service, ConfigMap, and HTTPRoute it renders from that CR plus
  the adopted `LiteLLMModel` / `LiteLLMMCPServer` CRs (`proxyRef: litellm`).
- Single replica: the previous `Recreate` strategy existed to avoid rolling-update races against
  Prisma schema migrations at startup; the operator's Deployment doesn't expose a strategy field, so
  a second replica during a rollout is a theoretical (not yet observed) risk worth watching if this
  is ever scaled up.
- State lives in the shared CloudNativePG `postgres18` cluster (its own `litellm` database + role).
  The database/role are provisioned once, out of band — there is no init container recreating them on
  every reconcile — so the app is PVC-less and needs no VolSync.
- Response cache in the shared Dragonfly over `*.svc.cluster.local`, with a deliberately short TTL so
  usage tracking stays accurate.
- `generalSettings.store_model_in_db: false`: the `LiteLLMModel` CRs are the model source of truth,
  rendered into `config.yaml` by the operator (`applyMode: file`). This trades the admin-UI
  "add a model" workflow for GitOps-reviewable model changes.
- Prometheus success/failure callbacks enabled for metrics.
- Auth is the built-in master key (`apiAccess.masterKeyRef`) + UI credentials (no external SSO for an
  internal-only service). Existing DB-stored virtual keys (created before this migration) keep
  working unchanged — they authenticate against the DB, independent of `store_model_in_db`.
- Routing is an internal-only HTTPRoute (operator-managed via `spec.route`) on the `envoy-internal`
  listener at `litellm.${SECRET_DOMAIN}`; the API (`/v1/*`) and admin UI (`/ui`) share one port.
- Secrets come from an ExternalSecret pulling the `litellm` 1Password item (Talos vault).

## Deploy gotchas

- **Prometheus scrape:** the `/metrics` endpoint needs `require_auth_for_metrics_endpoint: false`,
  **and** the ServiceMonitor path must be `/metrics/` (trailing slash, with redirect-follow off).
  Otherwise the scrape silently gets nothing.
- The liveness/readiness probe path is `/health/liveliness` (upstream's spelling); the operator
  defaults readiness to `/health/readiness` instead, so both probes are set explicitly in
  `litellmproxy.yaml`. `/v1/health` requires auth and runs heavy backend probes, too heavy for
  kubelet.
- Never rotate `LITELLM_SALT_KEY`: rotating it makes DB-stored provider/virtual keys undecipherable
  (config-file keys are unaffected). Note this on the 1Password item.
- Keep provider API keys out of git by referencing them as `os.environ/<NAME>` in a `LiteLLMModel`'s
  `params.apiKey` (the operator wires the env var from a Secret automatically when you use
  `apiKeyRef` instead of a literal `os.environ/` string).
- Two `GrafanaDashboard` CRs: `litellm` is fetched from grafana.com via URL (Flux `postBuild` does
  not process remotely fetched JSON, so no `$${var}` escaping is needed); `litellm-detail` is
  vendored JSON in a `configMapGenerator` with the
  `kustomize.toolkit.fluxcd.io/substitute: disabled` annotation, since that dashboard's JSON has no
  `${VAR}` placeholders needing Flux substitution and the annotation stops Flux blanking any stray
  `${...}` inside it.
- The dashboard ConfigMap and every `LiteLLMProxy`/`LiteLLMModel`/`LiteLLMMCPServer` CR set
  `metadata.namespace` explicitly (Checkov CKV_K8S_21 for the ConfigMap; the CRs are namespaced by
  convention with the rest of the app).

## Operational notes

- Reconcile chain: `flux reconcile source git flux-system` → `external-secrets` →
  `onepassword` → `litellm-operator` → the `litellm` Kustomization → its `LiteLLMProxy`.
- `kubectl get litellmproxy litellm -n ai` shows the `Ready` condition, `observedModels` count, and
  `readyReplicas`; `kubectl describe` surfaces render/apply errors (e.g. a model referencing a
  Secret key that doesn't exist) that never reach the pod at all.
- API round-trip test:

  ```bash
  curl -sS https://litellm.${SECRET_DOMAIN}/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"<model-group>","messages":[{"role":"user","content":"ping"}]}'
  ```

- Add models via a `LiteLLMModel` CR under `litellm/app/models/` (`proxyRef: litellm`). The
  operator hashes the rendered config into a pod annotation and rolls the Deployment automatically
  — no manual restart or Reloader annotation needed.
- The Grafana "LiteLLM" / "LiteLLM Detail" dashboards and the Gatus check confirm health and metrics
  flow.
