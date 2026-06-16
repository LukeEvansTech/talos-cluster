# Troubleshooting

This section tracks known issues and their workarounds in the cluster, split into one knowledge-base entry per issue. Each entry follows the same shape: **Symptom → Cause → Fix**.

## Symptom ladder

Work down from what you observe to the most likely entry:

- **Secrets / 1Password**
    - PushSecret logs spurious HTTP 400 errors but status shows `Synced` → [KB-001](kb/001-1password-connect-pushsecret-false-400-errors.md)
- **Plex playback**
    - 4K direct-play freezes for ~60s every ~6 minutes on LAN Apple TVs → [KB-002](kb/002-plex-direct-play-buffering-bbr-mtu-probing.md)
    - "Server unavailable" / connection drops at session start, pod otherwise healthy → [KB-003](kb/003-plex-advertises-broken-connection-urls.md)
- **Talos upgrades**
    - TUPPR patch rollout stuck after drain; node cordoned and still on the old version → [KB-004](kb/004-talos-patch-rollout-gotchas-tuppr.md)
- **CI / validation**
    - `Flate - Test` fails or skips on the `gpu-operator` namespace → [KB-005](kb/005-flate-misresolves-ngc-helmrepository-chart-urls.md)
    - Checkov CKV_K8S_21 flags a namespaced resource as `default` → [KB-006](kb/006-checkov-ckv-k8s-21-namespaced-resources.md)

## All entries

- [KB-001: 1Password Connect PushSecret False 400 Errors](kb/001-1password-connect-pushsecret-false-400-errors.md)
- [KB-002: Plex Direct-Play Buffering on LAN Apple TVs (BBR + MTU Probing)](kb/002-plex-direct-play-buffering-bbr-mtu-probing.md)
- [KB-003: Plex Advertises Broken Connection URLs To plex.tv](kb/003-plex-advertises-broken-connection-urls.md)
- [KB-004: Talos Patch Rollout Gotchas (TUPPR)](kb/004-talos-patch-rollout-gotchas-tuppr.md)
- [KB-005: flate Mis-Resolves NGC HelmRepository Chart URLs](kb/005-flate-misresolves-ngc-helmrepository-chart-urls.md)
- [KB-006: Checkov CKV_K8S_21 Flags Namespaced Resources Without an Explicit Namespace](kb/006-checkov-ckv-k8s-21-namespaced-resources.md)
