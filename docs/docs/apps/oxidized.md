# Oxidized

[Oxidized](https://github.com/ytti/oxidized) is a network device configuration backup tool. It runs
as a single pod in the `observability` namespace, polls the home network devices on a daily interval,
stores each running config in a local bare Git repository on a Ceph PVC, and pushes diffs to a private
GitHub repository. It is the drift-detection complement to the IaC that describes the network's
*desired* state: Oxidized snapshots *actual* running state, catching out-of-band changes (manual
GUI/SSH edits) that bypass the IaC.

## Purpose

- Back up the running configuration of the home network's infrastructure devices so config changes are
  versioned, diffable, and recoverable.
- Detect drift between IaC-declared intent and what's actually running on the hardware.
- Notify on change and on failure, and surface per-device freshness as metrics.
- Device roles backed up (described generically — this is a public repo): the perimeter
  firewall/router, and the PoE and non-PoE access-switch pair — exactly three devices, wired
  individually in the ExternalSecret's `router.db` template.
- Deliberately not included: a core switch and a wireless AP controller, because Oxidized ships no
  built-in model for either — adding them would mean authoring and maintaining custom device
  models, which isn't worth it for two devices.
- Out of scope: SNMP-only gear (PDUs) with no config dump that fits Oxidized, and cloud services
  already managed by external IaC.

## Design decisions

- **bjw-s `app-template` single pod, two containers + one init container.** The `app` container runs
  Oxidized (REST on `:8888`); a sidecar exporter scrapes Oxidized's `/nodes.json` and exposes
  per-device last-success metrics on `:8080`. One init container (`ssh-setup`) stages the SSH deploy
  key onto a memory-backed `emptyDir` before the app starts.
- **The device inventory/lookup table (`router.db`) and device credentials are rendered INSIDE the
  ExternalSecret `target.template.data` block using Golang template syntax (`{{ .VAR }}`), then
  mounted directly from the rendered Secret — NEVER stored in a ConfigMap in git.** This is the
  repository-wide rule for any device address or credential table. In this app it is realised as:
  - The ExternalSecret renders `router.db` inline in its `target.template.data` block, substituting
    per-device hostnames, usernames, and passwords from the 1Password item using `{{ .VAR }}`
    Golang template syntax.
  - The rendered `router.db` is mounted directly from the Secret (`type: secret`, name:
    `oxidized-secret`) at `/etc/oxidized/router.db.d/router.db` as a read-only file. Net effect:
    device credentials never land on the Ceph PVC and never appear in git.
  - The ConfigMap holds only the Oxidized YAML config (not a `router.db` template). There is no
    `envsubst` init container for `router.db`.
- **Secrets flow 1Password → ExternalSecret → Secret.** A single 1Password item (in the `Talos` vault,
  the only vault the cluster's `ClusterSecretStore` reads) carries per-device username/password pairs,
  the GitHub deploy key, the `known_hosts` line, and the notification provider token/user key.
- **SSH key only on tmpfs.** The SSH deploy key is written by the `ssh-setup` init container to a
  memory-backed `emptyDir` (`~/.ssh`). The Ceph PVC holds only Oxidized state and the bare Git
  repo.
- **Push target is a private GitHub repo over SSH.** An `exec` hook (not the `githubrepo` hook)
  fires on `post_store` and runs system git over SSH using the ed25519 deploy key staged by the init
  container. The `githubrepo` hook (rugged/libgit2) was abandoned because it fails against current
  GitHub with `Rugged::SshError: remote rejected authentication`. The repo stays private — even after
  secret stripping, configs may carry topology/address detail.
- **Notifications are split.** Drift and poll-failure events fire a direct `exec` hook to the push
  notification provider; longer-horizon staleness is alerted through the existing
  kube-prometheus-stack → Alertmanager route.
- **Partially hardened containers.** The init container (`ssh-setup`) and the `exporter` sidecar
  run as uid/gid `30000` with `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, all capabilities
  dropped, and `seccompProfile: RuntimeDefault`. The main `app` container must start as root
  (`runAsUser: 0`, `runAsNonRoot: false`, `readOnlyRootFilesystem: false`) because the oxidized image
  bootstraps via runit/runsvdir before using `gosu` to drop to uid `30000` at runtime; it is granted
  the minimal capabilities needed for that handover (`CHOWN`, `SETUID`, `SETGID`) and all others are
  dropped.
- **Backups + routing.** The state PVC is `ceph-block` (RWO) and snapshotted by the VolSync component
  (`VOLSYNC_CAPACITY: 1Gi`). The UI is reached via an internal `HTTPRoute` on `envoy-internal`
  (`oxidized.${SECRET_DOMAIN}`). Both images are Renovate-tracked and digest-pinned.

## Deploy gotchas

- **`router.db` map must match the Oxidized config delimiter.** The CSV source uses a `:`-delimited
  `name:ip:model:username:password` layout; the `source.csv.map` and `delimiter` in the Oxidized config
  must agree exactly or every device parses wrong.
- **Per-device, not shared, credentials.** Some device roles that look like a matched pair (e.g. two
  switches) have *separate* credentials in 1Password. Don't collapse them into one
  username/password — split into per-device secret keys, and make sure the ExternalSecret template
  and `router.db` rendering all use the same per-device key names.
- **Init-container and Secret mount.** The `ssh-setup` init writes the deploy key to a memory
  `emptyDir`; `router.db` is mounted directly from the rendered Secret. A failed init is usually a
  malformed deploy key; if `router.db` is missing or empty, the ExternalSecret hasn't synced yet
  (check `SecretSynced` vs `SecretSyncError`).
- **`rest: 0.0.0.0:8888` is required.** Without the REST listener the HTTPRoute, the exporter
  (`-U http://localhost:8888`), and the readiness probe (`/nodes.json`) all have nothing to talk to.
- **Pin image digests and confirm the exporter tag exists.** Pin all three images
  (Oxidized, exporter, and the init base image) to `@sha256:` digests. The exporter's "latest"
  release tag on the registry may lag its GitHub release — pull the digest for a tag that actually
  has a published image, or Renovate can't track it.
- **Confirm the AP-controller model string before first poll.** The correct Oxidized model name
  depends on the controller's product variant; an unverified guess just makes that one device's polls
  fail. Fixing it is a one-line edit to the `router.db` entry in the ExternalSecret template, then
  waiting for the Secret to re-sync.
- **Reachability + DNS are preconditions.** The pod must be able to reach each device on SSH (and HTTPS
  where used), reach `github.com:22` and the notification API, and resolve the internal hostnames. A
  blocked path is almost always a missing firewall rule from the cluster pod CIDR to the device VLAN;
  if internal DNS doesn't resolve for the pod, fall back to IP addresses in `router.db`.
- **Notification token vs user key.** The push provider needs a per-application token (created in the
  provider's UI, not via API) *and* a user key — swapping the two yields silent non-delivery while Git
  commits still succeed.

## Operational notes

- **Metrics + alerts.** The exporter exposes `oxidized_device_status` (a gauge labeled `full_name`,
  `name`, `group`, `model`; `2`=success, `1`=never, `0`=no_connection). A `PrometheusRule` alerts
  `OxidizedDeviceStale` when status is `!= 2` for 48h (with daily polls that's two missed cycles) and
  `OxidizedDown` when `up{job=~".*oxidized.*"} == 0` for 15m.
- **Validation cadence trick.** For an initial deploy, temporarily set `interval: 60` (1-minute poll)
  for fast feedback, verify every device goes green and commits land, then restore `interval: 86400`
  before merge. The same fast-feedback trick applies to alert `for:` thresholds.
- **Audit committed configs for plaintext secrets.** After the first successful poll, clone the backup
  repo and grep the firewall config in particular for plaintext secret tags. If anything sensitive
  leaks through, enable Oxidized's `remove_secret` (globally and/or per-model, with custom regex for
  any tag the built-in logic misses), redeploy the ConfigMap, and re-verify the next cycle is clean.
- **Failure modes are mostly self-healing.**
  - A single unreachable device fires the failure hook and, after 48h, the stale alert; it recovers on
    its own when the device returns.
  - A failed GitHub push just accumulates local commits that flush on the next successful push.
  - A notification-provider outage loses only the notification — drift commits are still stored in Git.
  - 1Password Connect being down leaves the already-mounted Secret working for hours.
- **State recovery.** If the PVC is corrupted, delete it and either restore via VolSync or re-clone
  from the GitHub backup repo — the remote history is durable and a redeploy picks up where it left
  off. Uninstall is `just kube delete-ks observability oxidized` (+ optional PVC delete); the 1Password
  item and GitHub repo persist for clean redeploy.
- **Restart after ConfigMap-only changes.** Editing the Oxidized config in the ConfigMap does not roll
  the pod automatically — re-apply the Kustomization or restart the deployment so the new content is
  picked up. Changes to `router.db` require updating the ExternalSecret template and waiting for the
  Secret to re-sync (or force-syncing via `just kube sync-es`).
