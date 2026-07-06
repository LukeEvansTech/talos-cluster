# Metabase

Open-source business-intelligence / dashboarding app in the `database` namespace, backed by the
shared `postgres18` CloudNativePG cluster.

## Purpose

- Provide self-service analytics over the cluster's Postgres datasets (security findings, app DBs,
  pgAdmin-managed databases) without requiring everyone to write SQL.
- Stateless pod: all Metabase application state (questions, dashboards, saved DB connections) lives in
  a dedicated database on `postgres18`, so no PVC is needed. `/plugins` and `/tmp` are `emptyDir`.

## Design decisions

- **CloudNativePG, not embedded H2.** Metabase's own docs warn against H2 in production (corruption
  risk, no concurrent access). Reusing the hardened, replicated, backed-up cluster costs one extra
  database; app data is captured by the existing CNPG backup pipeline (Barman + scheduled remote).
- **bjw-s `app-template` chart**, like every other app in the namespace — one Deployment + Service +
  route + `postgres-init` initContainer fits it cleanly. The dedicated `metabase` DB and role are
  bootstrapped idempotently by the `ghcr.io/home-operations/postgres-init` initContainer on first run.
- **Internal-only ingress.** Inline `route:` on `envoy-internal` for both
  `metabase.${SECRET_DOMAIN}` and `metabase.${SECRET_INTERNAL_DOMAIN}`. Cloudflare-fronting can be
  added later by switching the `parentRef` and adding an external hostname.
- **Native Prometheus exporter, not a JMX sidecar.** Metabase 0.49+ ships a built-in `/metrics`
  endpoint via `MB_PROMETHEUS_SERVER_PORT` (port 9191 here), scraped by a `PodMonitor`; a community
  Grafana dashboard is imported via a `GrafanaDashboard` CR.
- **Deferred (YAGNI):** SMTP, SSO/LDAP/OIDC (paid edition only on OSS — first run creates a local
  admin), `/plugins` PVC for third-party JDBC drivers, and pre-seeded source DBs/dashboards. All are
  configurable later through the UI without redeploying.

## Deploy gotchas

- **JVM TLS rejects the CNPG server cert — use `sslmode=disable`.** Java's `X509Factory` rejects
  CloudNativePG's empty-issuer-DN server certificate, so a JVM app cannot do `verify-full` TLS to the
  `postgres18` cluster. Point Metabase at the in-cluster service with TLS verification off (the pod →
  `*-rw.svc.cluster.local` network path is already trusted):

  ```text
  MB_DB_CONNECTION_URI=jdbc:postgresql://postgres18-rw.database.svc.cluster.local:5432/metabase?sslmode=disable
  ```

  This applies to **any** JVM/JDBC app talking to CNPG, not just Metabase.

- **`--add-opens` is required for the metrics collector.** On JDK 17+ the native Prometheus collector
  needs `JAVA_OPTS: "--add-opens java.base/java.nio=ALL-UNNAMED"`, or `/metrics` fails to start.
  Future versions may need more `--add-opens` flags; the pod logs the JVM error on start, so append as
  needed.

- **Slow first-run cold start.** The first boot runs Liquibase migrations against an empty app DB
  (~60-90s). Size the startup probe for it (`failureThreshold: 60`, `periodSeconds: 5` against
  `/api/health`); a too-tight threshold CrashLoops the pod before it finishes migrating.

- **First-run migration can spike memory.** Set the memory limit well above Metabase's documented 1Gi
  floor (3Gi limit / 1.5Gi request here) so the migration doesn't OOM.

- **Secret key names are Metabase's, not the values defaults.** The ExternalSecret combines the
  per-app `metabase` 1Password item with the `cloudnative-pg` item, and must materialise the exact
  env names the container and initContainer read: `MB_DB_*` / `MB_ENCRYPTION_SECRET_KEY` for the app,
  `INIT_POSTGRES_*` (including `INIT_POSTGRES_SUPER_*`) for the init container. Generate the encryption
  key and DB password locally and create the 1Password item via `op item create`, never by hand.

## Operational notes

- **Never lose or naively rotate `MB_ENCRYPTION_SECRET_KEY`.** It encrypts the source-DB credentials
  stored inside Metabase's app DB; losing it means Metabase can no longer decrypt saved connections.
  The key's canonical home is 1Password — to rotate, run Metabase's `rotate-encryption-key` admin
  command first, then update the secret.
- **Recovery is via CNPG, not the app.** Since all state is in the `metabase` database on
  `postgres18`, a corrupt or failed schema migration is recovered by point-in-time restore of that DB
  from the CNPG backups — there is no app-side backup.
- **If `postgres18` is down at startup**, the `postgres-init` initContainer exits non-zero and the pod
  restarts with backoff; once Postgres returns, the next attempt succeeds with no manual intervention.
- **First-run setup is manual.** Browse to the internal hostname, complete the wizard to create the
  local admin, then add source databases through the UI.
- **Health checks:** liveness/readiness hit `GET /api/health` (returns `{"status":"ok"}`); Gatus
  monitors it automatically (the gatus-sidecar chart auto-discovers the HTTPRoute). Confirm metrics
  with a port-forward to 9191 and look for `jvm_memory_used_bytes` / `metabase_*` series.
