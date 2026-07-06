# Reclaimerr

Jellyfin/Plex disk-space reclamation tool in the `media` namespace. Identifies and deletes
unwatched or low-rated media, exposing an internal-only web UI via Envoy Gateway. Standard
bjw-s `app-template` deployment mirroring sibling apps (maintainerr, pulsarr).

## Purpose

- Reclaim disk by flagging and removing unwatched / low-rated media, integrating with
  Jellyfin/Plex and the \*arr stack (Sonarr/Radarr).
- Single Deployment pod with a small VolSync-backed PVC (`2Gi`) at `/app/data` holding the
  app database, logs, and static files.
- Jellyfin/Plex and \*arr connections are configured through the UI post-deploy, not in git.

## Design decisions

- **bjw-s `app-template` chart** (OCI source, tag matching maintainerr/pulsarr): the standard
  `ks.yaml` + `app/` (kustomization, ocirepository, helmrelease) layout. No deviation from the
  namespace pattern.
- **No ExternalSecret.** Reclaimerr auto-generates `JWT_SECRET` and `ENCRYPTION_KEY` on first
  launch and persists them into `/app/data` (the upstream-recommended path). This is simpler
  than 1Password management — at the cost that the keys live only in the PVC, so the PVC is the
  source of truth for sessions and encrypted state.
- **Internal-only ingress.** Inline `route:` on the `envoy-internal` listener (namespace
  `network`) for both `${SECRET_DOMAIN}` and `${SECRET_INTERNAL_DOMAIN}` hostnames. No
  Cloudflare/external exposure — consistent with maintainerr.
- **`COOKIE_SECURE: "true"`** because Envoy terminates TLS in front of it, and
  `CORS_ORIGINS` includes both the external-domain and internal-domain hostnames
  (`${SECRET_DOMAIN}` and `${SECRET_INTERNAL_DOMAIN}`) so the SPA's API calls are accepted
  regardless of which hostname the user browses to.
- **Components:** `homepage` (dashboard tile under the
  `Media` group, `mdi-broom` icon) and `volsync` (PVC backup). (Gatus monitoring is automatic via
  the gatus-sidecar chart's HTTPRoute auto-discovery — no `gatus/guarded` component.) `VOLSYNC_CAPACITY: 2Gi` and
  `VOLSYNC_CACHE_CAPACITY: 1Gi` are the required substitutes for the volsync component
  (overriding the 5Gi/10Gi defaults).
- **Deferred (out of scope):** external ingress, pre-seeding `JWT_SECRET`/`ENCRYPTION_KEY` via
  1Password, and a `TMDB_API_KEY` override (the bundled upstream key is fine).

## Deploy gotchas

- **`readOnlyRootFilesystem: true` needs a writable `/tmp`.** The container security context
  locks the root filesystem read-only, so a `tmp` `emptyDir` is mounted at `/tmp`. Without it the
  app fails to write scratch files at runtime. `/app/data` is writable via the VolSync PVC.
- **Health endpoint is a backend JSON route, not an SPA.** All three probes (liveness,
  readiness, startup) hit `GET /api/info/health` on port `8000`, and the Gatus condition is
  `[STATUS] == 200` (not a redirecting-SPA check). The Gatus endpoint targets the in-cluster
  service URL on `*.svc.cluster.local`.

  ```text
  httpGet: { path: /api/info/health, port: 8000 }
  ```

- **Startup probe must allow a slow first boot.** First launch generates the JWT/encryption
  keys and initialises the DB into the empty PVC; size the startup probe generously
  (`failureThreshold: 30`, `periodSeconds: 5`) so it does not CrashLoop before init finishes.
- **Beta image, pinned by digest.** The image is a `0.1.0-betaN@sha256:...` tag; Renovate tracks
  new releases. Keep the digest pin so a moving beta tag cannot silently change the running build.
- **Component path depth.** From `kubernetes/apps/media/reclaimerr/ks.yaml`, the component
  references need four `../` segments to reach `components/`. A wrong depth or a missing
  `VOLSYNC_CAPACITY` substitute fails the flate/flux render.

## Operational notes

- **The PVC is the only state.** Losing `/app/data` loses the auto-generated `JWT_SECRET` and
  `ENCRYPTION_KEY` (all sessions invalidated, encrypted config unreadable). Recovery is via the
  VolSync snapshot/backup of the `reclaimerr` PVC — there is no 1Password copy of the keys.
- **Runs unprivileged.** Pod security context is `runAsNonRoot` as UID/GID `1000` with
  `fsGroup: 1000` and `fsGroupChangePolicy: OnRootMismatch`; the container drops all
  capabilities and disables privilege escalation. Resources are light (`cpu` request `10m`,
  `memory` limit `512Mi`).
- **First-run setup is manual.** After the pod is `Ready`, browse to the internal hostname and
  wire up Jellyfin/Plex and \*arr connections through the UI; nothing is pre-seeded.
- **Verify after merge:** PVC `Bound`, pod `Running` / `Ready=1/1`, logs show it listening on
  `:8000` with keys generated into `/app/data`, the HTTPRoute is accepted by `envoy-internal`,
  and `GET /api/info/health` returns a `200` JSON body via the internal hostname.
- **Rollback:** `flux suspend helmrelease reclaimerr -n media` then delete the pod; reverting the
  commits prunes the Kustomization (`prune: true`) while VolSync snapshots the PVC per the
  volsync component defaults.
