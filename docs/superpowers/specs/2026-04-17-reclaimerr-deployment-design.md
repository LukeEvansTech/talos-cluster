# Reclaimerr Deployment Design

## Overview

Deploy [Reclaimerr](https://github.com/jessielw/Reclaimerr) — a Jellyfin/Plex-integrated disk-space reclamation tool that identifies and deletes unwatched or low-rated media — into the `media` namespace following the standard bjw-s app-template pattern used by sibling apps (maintainerr, pulsarr).

## Architecture

Standard app-template deployment (`ks.yaml` + 3 files in `app/`) in the `media` namespace. Web UI served internal-only via Envoy Gateway. Registered alphabetically in `kubernetes/apps/media/kustomization.yaml` (between `radarr` and `recommendarr`).

```
kubernetes/apps/media/reclaimerr/
  ks.yaml                   # Flux Kustomization
  app/
    kustomization.yaml      # Resource list
    helmrelease.yaml        # bjw-s/app-template HelmRelease
    ocirepository.yaml      # OCI chart source
```

No `ExternalSecret` — Reclaimerr auto-generates `JWT_SECRET` and `ENCRYPTION_KEY` on first launch and persists them in `/app/data` (the upstream-recommended path; simpler than 1Password management).

## Container Image

- **Repository:** `ghcr.io/jessielw/reclaimerr`
- **Tag (inline format):** `0.1.0-beta7@sha256:b41300380197333584c09effa16a4b10caa31806ad145c0c46a04f7124ed33a4`

(Beta version — Renovate will track new releases.)

## Chart Source

- `ocirepository.yaml`: `oci://ghcr.io/bjw-s-labs/helm/app-template` tag `4.6.2` (matches maintainerr/pulsarr).

## Flux Kustomization (`ks.yaml`)

- `dependsOn`: `rook-ceph-cluster` (for VolSync-backed PVC).
- Components:
  - `../../../../components/gatus/guarded`
  - `../../../../components/homepage`
  - `../../../../components/volsync`
- `postBuild.substitute`:
  - `APP: reclaimerr`
  - `VOLSYNC_CAPACITY: 2Gi` (DB + logs + static files)
  - `HOMEPAGE_NAME: Reclaimerr`
  - `HOMEPAGE_GROUP: Media`
  - `HOMEPAGE_ICON: mdi-broom`
  - `HOMEPAGE_DESCRIPTION: Disk space reclamation`

## HelmRelease Spec

### Controller
- Single pod (default Deployment).

### Container
- **Env:**
  - `TZ: ${TIMEZONE}`
  - `API_HOST: 0.0.0.0`
  - `API_PORT: &port 8000`
  - `COOKIE_SECURE: "true"` (served over HTTPS by Envoy Gateway)
  - `CORS_ORIGINS: https://reclaimerr.${SECRET_DOMAIN}`
  - `LOG_LEVEL: INFO`
- **Probes** (liveness/readiness/startup):
  - `httpGet` path `/api/info/health` on port 8000 — upstream-provided health endpoint.
  - Startup: 30 × 5s.
- **Container security context:** `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities: {drop: ["ALL"]}`.
- **Resources:** `requests.cpu: 10m`, `limits.memory: 512Mi`.

### Pod
- `runAsNonRoot: true`, `runAsUser: 1000`, `runAsGroup: 1000`, `fsGroup: 1000`, `fsGroupChangePolicy: OnRootMismatch`.

### Service
- `http: 8000`.

### Route
- `parentRefs: envoy-internal` (internal network only — consistent with maintainerr).
- Hostnames: `{{ .Release.Name }}.${SECRET_DOMAIN}` and `{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}`.
- Gatus annotation: `conditions: ["[STATUS] == 200"]` (backend provides JSON health endpoint, not a redirecting SPA).

### Persistence
- `config`: `existingClaim: "{{ .Release.Name }}"` mounted at `/app/data` (VolSync-managed PVC, 2Gi).
- `tmp`: `emptyDir` mounted at `/tmp` (needed because `readOnlyRootFilesystem: true`).

## Namespace Registration

Add `./reclaimerr/ks.yaml` to `kubernetes/apps/media/kustomization.yaml` between `radarr` and `recommendarr` (alphabetical).

## Validation

- `flux-local test --all-namespaces --enable-helm --path kubernetes/flux/cluster --verbose` must pass.
- After merge: verify PVC bound, pod `Ready`, HTTPRoute accepted, `/api/info/health` returns 200 via internal hostname.

## Out of Scope

- External (Cloudflare) ingress — internal only.
- Pre-seeding `JWT_SECRET`/`ENCRYPTION_KEY` via 1Password.
- TMDB API key override (`TMDB_API_KEY`) — default upstream key is fine.
- Configuring Reclaimerr's Jellyfin/Plex/*arr connections — done via UI post-deploy.
