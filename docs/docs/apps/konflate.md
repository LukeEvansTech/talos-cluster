# Konflate

## Purpose

Konflate is this repository's own in-cluster PR render tool: it renders the Flux config at each
open PR's merge-base and head, then posts the rendered Kubernetes diff as a `Konflate` commit
status and PR comment. `README.md` in the app directory covers what it does, how it is reached,
and its anonymous vs. webhook-driven modes. This page holds the configuration reasoning that used
to sit in HelmRelease comments.

## Configuration rationale

- **Auth.** The `konflate-bot` GitHub App identity (`KONFLATE_APP_CLIENT_ID` /
  `KONFLATE_APP_PRIVATE_KEY`) lifts the anonymous API rate limit and signs the write-back for
  commit statuses and PR comments (#3033). A GitHub fine-grained PAT (`KONFLATE_TOKEN`,
  public-repo read-only) stays configured as a fallback beside it; it was originally meant to be
  dropped once the CI workflows that depended on it were cut over (#3375), but it is still present
  and its removal was never revisited.
- **Refresh.** `refreshInterval: 15m` is the backstop poll that catches a missed webhook delivery
  (#2985); `KONFLATE_WEBHOOK_SECRET` (HMAC) lets GitHub push an instant refresh on every push
  instead of waiting for the poll. `KONFLATE_PUSH_TOKEN` (`POST /api/prs/{n}/refresh`) started as
  the CI-triggered refresh and is now a fallback for a manual or scripted refresh, since the
  webhook took over as the primary trigger.
- **Native status and PR comments.** `statusChecks` and `prComments` make konflate post the
  `Konflate` commit status and PR summary comment itself; this replaced the `flate.yaml` and
  `konflate.yaml` GitHub Actions workflows that used to fetch and post the same summary, both
  dropped in #3375. `statusCheckName` is left unset, so the check name defaults to `Konflate`.
- **Cluster capabilities.** Konflate templates charts offline with no API server to query, so
  without `config.kubeVersion` and `config.helmApiVersions` it renders against helm's built-in
  core API set and whatever Kubernetes version the helm SDK was built against. A chart gated on
  `.Capabilities.APIVersions.Has` or `.Capabilities.KubeVersion` then renders differently in a PR
  diff than it does once Flux applies it, which is the gap the diff is meant to catch (#5109). Both
  values are templated from the HelmRelease's own `.Capabilities`, so helm-controller
  resolves them against this cluster, which is also konflate's render target, with nothing static
  to keep in sync.
- **Cache.** `persistence.enabled: false` mounts an emptyDir on the node filesystem instead of a
  PVC. The fixed-inode `ceph-block` RBD volume it replaced hit
  [KB-011](../troubleshooting/kb/011-konflate-render-failures.md): konflate's source, render, and
  stage caches are millions of tiny files, so they exhaust an RBD volume's inodes long before its
  byte budget. The trade-off is that open-PR diffs re-render and the merged-PR shelf is lost on a
  konflate restart, acceptable for a diff UI.
- **Memory.** With persistence off, a restart re-renders every open PR from cold, around 18
  concurrent renders at the time this was sized; that OOM-killed the pod at a 1Gi limit, so it was
  raised to 2Gi. `GOMEMLIMIT` is derived from the memory limit at 90%, so Go's garbage collector
  tracks the container's ceiling rather than the host's.

## External exposure

Two external HTTPRoutes exist beside the chart-managed internal one:

- `httproute-webhook.yaml` exposes only `POST /hooks` (HMAC-validated) so GitHub can deliver
  webhook events (#2985).
- `httproute.yaml` exposes the whole konflate UI externally on `envoy-external`. It was added
  (#3020) so a GitHub Actions job running on a standard `ubuntu-latest` runner could reach the
  summary API over the public internet. That job was removed when konflate started posting its
  own commit status and PR comment in-cluster (#3375), so the reason this route was added no
  longer applies. Whether to narrow or remove it has not been revisited, and the app's `README.md`
  still describes access as internal-only and does not mention this route.

## References

- konflate: <https://github.com/home-operations/konflate>
- [KB-011: konflate Render Failures (Cache Inode Fill / Phantom Mirror)](../troubleshooting/kb/011-konflate-render-failures.md)
