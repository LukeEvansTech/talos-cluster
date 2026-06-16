# LiteLLM

## Purpose

LiteLLM is an OpenAI-compatible LLM gateway in the `ai` namespace. It fronts the local Ollama
instances and cloud providers behind one API, so in-cluster consumers target a single endpoint and
get virtual keys, per-key budgets, request logging, model-group routing, fallbacks, and an admin UI.
See the [AI / LLM stack](../architecture/ai-llm-stack.md) page for how it fits the wider stack.

## Design decisions

- Deployed with the bjw-s `app-template` chart via a per-app `OCIRepository`.
- Single replica with the `Recreate` strategy — avoids rolling-update races against the Prisma
  schema migrations that run at startup.
- State lives in the shared CloudNativePG `postgres18` cluster (its own `litellm` database + role,
  created idempotently by a `postgres-init` init container) — so the app is PVC-less and needs no
  VolSync.
- Response cache in the shared Dragonfly over `*.svc.cluster.local`, with a deliberately short TTL so
  usage tracking stays accurate.
- `STORE_MODEL_IN_DB: "True"` so models added through the UI persist and merge with the seed
  `config.yaml` (model-groups, fallbacks, retries, request timeout, MCP).
- Prometheus success/failure callbacks enabled for metrics.
- Auth is the built-in master key + UI credentials (no external SSO for an internal-only service).
- Routing is an internal-only HTTPRoute on the `envoy-internal` listener at
  `litellm.${SECRET_INTERNAL_DOMAIN}`; the API (`/v1/*`) and admin UI (`/ui`) share one port.
- Secrets come from an ExternalSecret pulling the `litellm` 1Password item (Talos vault).

## Deploy gotchas

- **Prometheus scrape:** the `/metrics` endpoint needs `require_auth_for_metrics_endpoint: false`,
  **and** the ServiceMonitor path must be `/metrics/` (trailing slash, with redirect-follow off) —
  otherwise the scrape silently gets nothing.
- The liveness/readiness probe path is `/health/liveliness` (upstream's spelling). `/v1/health`
  requires auth and runs heavy backend probes — too heavy for kubelet.
- Never rotate `LITELLM_SALT_KEY` — rotating it makes UI-added provider keys undecipherable
  (config-file keys are unaffected). Note this on the 1Password item.
- Keep provider API keys out of git and out of the rendered ConfigMap by referencing them as
  `os.environ/<NAME>` in `config.yaml`.
- In the Grafana dashboard JSON, escape template variables `${var}` as `$${var}` so Flux `postBuild`
  does not strip them; use `"datasource": null` rather than a hard-coded UID.
- The ConfigMap must set `metadata.namespace` explicitly (Checkov CKV_K8S_21).

## Operational notes

- Reconcile chain: `flux reconcile source git flux-system` → `external-secrets` →
  `onepassword` → the `litellm` Kustomization → its HelmRelease.
- Check the `init-db` container logs to confirm the role/database were created (idempotent), then the
  app logs for the Prisma migration and the listener coming up.
- API round-trip test:

  ```bash
  curl -sS https://litellm.${SECRET_INTERNAL_DOMAIN}/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"<model-group>","messages":[{"role":"user","content":"ping"}]}'
  ```

- Add models through the admin UI (persisted via `STORE_MODEL_IN_DB`). A `config.yaml` ConfigMap
  change needs a `kubectl rollout restart deploy/litellm -n ai` (or rely on the reloader annotation).
- The Grafana "LiteLLM" dashboard and the Gatus check confirm health and metrics flow.
