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
- Device roles backed up (described generically — this is a public repo):
  - the perimeter firewall/router
  - the core switch
  - the access/PoE switches
  - the wireless AP controller
- Out of scope: SNMP-only gear (PDUs) with no config dump that fits Oxidized, and cloud services
  already managed by external IaC.

## Design decisions

- **bjw-s `app-template` single pod, two containers + two init containers.** The `app` container runs
  Oxidized (REST on `:8888`); a sidecar exporter scrapes Oxidized's `/nodes.json` and exposes
  per-device last-success metrics on `:8080`. Two init containers stage secrets onto memory-backed
  `emptyDir`s before the app starts.
- **The device inventory/lookup table (`router.db`) and device credentials are templated INSIDE the
  ExternalSecret `target.template.data` block and mounted from the rendered Secret — NEVER rendered
  into a ConfigMap in git.** This is the repository-wide rule for any device address or credential
  table. In this app it is realised as:
  - The ConfigMap holds only a `router.db` *template* with `${VAR}` placeholders (no real addresses or
    credentials).
  - An init container reads that template and runs `envsubst` against env vars sourced from the
    ExternalSecret-built Secret, writing the rendered `router.db` to a `tmpfs` `emptyDir` (memory).
  - The main container mounts that `tmpfs` read-only. Net effect: device credentials never land on the
    Ceph PVC and never appear in the ConfigMap or in git.
  - Flux `postBuild` leaves the device `${VAR}` placeholders literal because they don't exist in
    `cluster-secrets` (only `${SECRET_DOMAIN}` is substituted at apply time); the init container does
    the real substitution at boot.
- **Secrets flow 1Password → ExternalSecret → Secret.** A single 1Password item (in the `Talos` vault,
  the only vault the cluster's `ClusterSecretStore` reads) carries per-device username/password pairs,
  the GitHub deploy key, the `known_hosts` line, and the notification provider token/user key.
- **Secrets only on tmpfs.** The SSH deploy key is written by an init container to a memory-backed
  `emptyDir` (`~/.ssh`), and the rendered `router.db` lives on another. The Ceph PVC holds only
  Oxidized state and the bare Git repo.
- **Push target is a private GitHub repo over SSH.** Oxidized's `githubrepo` hook pushes on
  `post_store` using a dedicated write-scoped ed25519 deploy key. The repo stays private — even after
  secret stripping, configs may carry topology/address detail.
- **Notifications are split.** Drift and poll-failure events fire a direct `exec` hook to the push
  notification provider; longer-horizon staleness is alerted through the existing
  kube-prometheus-stack → Alertmanager route.
- **Hardened containers.** All containers run `runAsNonRoot` (uid/gid `30000`),
  `readOnlyRootFilesystem`, drop all capabilities, and use `seccompProfile: RuntimeDefault`.
- **Backups + routing.** The state PVC is `ceph-block` (RWO) and snapshotted by the VolSync component
  (`VOLSYNC_CAPACITY: 1Gi`). The UI is reached via an internal `HTTPRoute` on `envoy-internal`
  (`oxidized.${SECRET_DOMAIN}`). Both images are Renovate-tracked and digest-pinned.

## Deploy gotchas

- **`router.db` map must match the Oxidized config delimiter.** The CSV source uses a `:`-delimited
  `name:ip:model:username:password` layout; the `source.csv.map` and `delimiter` in the Oxidized config
  must agree exactly or every device parses wrong.
- **Per-device, not shared, credentials.** Some device roles that look like a matched pair (e.g. two
  switches) have *separate* credentials in 1Password. Don't collapse them into one
  username/password — split into per-device secret keys, and make sure the ExternalSecret template,
  init-container env vars, and `router.db` template all use the same per-device key names.
- **Init-container ordering and mounts.** The key-staging init writes to one memory `emptyDir`; the
  `router.db` render init writes to another; the app mounts both read-only. A failed first init is
  usually a malformed deploy key; a failed render init is usually a missing env var, i.e. the
  ExternalSecret hasn't synced yet (`SecretSynced` vs `SecretSyncError`).
- **`rest: 0.0.0.0:8888` is required.** Without the REST listener the HTTPRoute, the exporter
  (`-U http://localhost:8888`), and the readiness probe (`/nodes.json`) all have nothing to talk to.
- **Pin image digests and confirm the exporter tag exists.** Pin all four images
  (Oxidized, exporter, and the two init base images) to `@sha256:` digests. The exporter's "latest"
  release tag on the registry may lag its GitHub release — pull the digest for a tag that actually
  has a published image, or Renovate can't track it.
- **Confirm the AP-controller model string before first poll.** The correct Oxidized model name
  depends on the controller's product variant; an unverified guess just makes that one device's polls
  fail. Fixing it is a one-line `router.db` template edit + ConfigMap redeploy.
- **Reachability + DNS are preconditions.** The pod must be able to reach each device on SSH (and HTTPS
  where used), reach `github.com:22` and the notification API, and resolve the internal hostnames. A
  blocked path is almost always a missing firewall rule from the cluster pod CIDR to the device VLAN;
  if internal DNS doesn't resolve for the pod, fall back to addresses in the rendered `router.db`.
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
- **Restart after ConfigMap-only changes.** Editing the Oxidized config or `router.db` template in the
  ConfigMap does not roll the pod automatically — re-apply the Kustomization or restart the deployment
  so the new content is picked up.
