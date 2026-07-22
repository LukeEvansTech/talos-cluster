# iSponsorBlockTV

Headless background daemon (`media` namespace) that connects to YouTube TV apps on
local-network smart-TV / streaming devices and auto-skips sponsor segments using the
SponsorBlock community database. No web UI, no API.

## Purpose

- Skip sponsor (and other community-flagged) segments on YouTube TV-app playback across
  the household's TVs and streaming boxes. It runs fully automatically, with no user
  interaction during playback.
- Runs as a long-lived daemon: it watches paired devices over the LAN and issues skip
  commands; there is nothing to browse to.

## Design decisions

- Standard bjw-s `app-template` deployment (`ks.yaml` + `app/` with
  `ocirepository.yaml`, `helmrelease.yaml`, `kustomization.yaml`); single Deployment,
  1 replica.
- Image is the upstream `ghcr.io/dmunozv04/isponsorblocktv`, pinned by tag plus digest.
- No `Service` and no `HTTPRoute`: the daemon exposes no ports and has no web UI, so
  there is nothing to route. It only needs ordinary pod networking to reach the TVs.
- No `ExternalSecret`: there are no upstream credentials. The only config
  (`config.json`, including device pairings) is produced by the app's own interactive
  pairing flow, so it is not stored in 1Password.
- All probes (liveness / readiness / startup) disabled: no HTTP endpoint exists to
  health-check.
- VolSync component enabled (config PVC mounted at `/app/data`). Re-pairing every device
  by hand is tedious, so the pairings are backed up and survive cluster rebuilds. This is
  the main reason the app has a PVC at all.
- Hardened pod: `runAsNonRoot` (uid/gid 1000), `readOnlyRootFilesystem: true`, all
  capabilities dropped, `seccompProfile: RuntimeDefault`. An `emptyDir` is mounted at
  `/tmp` so the read-only rootfs still has scratch space.
- `reloader.stakater.com/auto: "true"` so config changes roll the pod.
- Tiny footprint: 10m CPU request, 128Mi memory request, 256Mi limit. `TZ` from
  `${TIMEZONE}`.

## Deploy gotchas

- The Flux Kustomization `dependsOn` `rook-ceph-cluster` (the VolSync config PVC lands on
  `ceph-block`); `wait: false`. VolSync substitutes (`APP`, `VOLSYNC_CAPACITY: 1Gi`,
  `VOLSYNC_CACHE_CAPACITY: 8Gi`) live in `ks.yaml`, and the `volsync` component goes in
  `ks.yaml` `spec.components` only (never also in `app/kustomization.yaml`).
- The pod `CrashLoopBackOff`s until a valid `config.json` exists. This is expected on
  first deploy, before any device is paired. It is not a failure; it clears once pairing
  is done.
- The config PVC uses `existingClaim` (VolSync owns the claim), mounted at the app's data
  directory `/app/data`. The pairing TUI writes `config.json` there.
- Register the app in the namespace `kustomization.yaml` in alphabetical order, same as
  every other `media` app.

## Operational notes

- First-run pairing is a manual, interactive step after Flux reconciles. Exec the setup
  TUI and follow the YouTube TV link-code prompts:

  ```bash
  kubectl exec -it -n media deploy/isponsorblocktv -- iSPBTV setup
  ```

  Each device shows a link code in its YouTube app; enter it in the TUI to pair. Repeat
  per device.
- Confirm it is working by tailing logs. Expect connected-device lines and
  segment-skip activity:

  ```bash
  kubectl logs -n media deploy/isponsorblocktv --tail=20
  ```

- Immediately after pairing, force a backup so the new `config.json` is captured rather
  than waiting for the next scheduled snapshot:

  ```bash
  just kube snapshot
  ```

- Re-pairing is only needed if the config PVC is lost; restoring the VolSync snapshot
  brings the pairings back without touching the TVs.
