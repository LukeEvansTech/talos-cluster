# Gatus

## Purpose

Gatus is the cluster's health-check and status-page tool, deployed via the `gatus-sidecar` chart,
which bundles Gatus with an endpoint-discovery sidecar and replaces an older app-template plus
initContainer setup (upstream `home-operations/gatus-sidecar` #11077). Endpoint configuration lives
in `resources/config.yaml`, shipped verbatim into a ConfigMap by a `configMapGenerator` and treated
as rendered content: this page does not describe its contents.

## Backups

Backups reference the `volsync/nfs` and `volsync/remote` components directly instead of the shared
`volsync` component. The shared component also ships a `pvc.yaml` that declares a PVC named
`${APP}` with a `dataSourceRef` pointing at `${APP}-dst`, but the Gatus PVC already exists, created
by the `gatus-sidecar` chart's own `persistence.enabled`. Pulling in the whole component would
collide on that PVC name and try to re-provision it from a `ReplicationDestination` that has no
snapshot yet. Referencing the two halves directly instead gives `ReplicationSource` +
`ReplicationDestination` + the kopia repository `ExternalSecret`, and leaves the live volume
untouched.

## Split-DNS check addresses

Two endpoints in `resources/config.yaml` check split-DNS addresses (`OPNSENSE_ADDR`,
`SVC_IMGUR_PROXY_ADDR`). The Gatus ConfigMap opts out of Flux's `postBuild` substitution
(`kustomize.toolkit.fluxcd.io/substitute: disabled`), so `${VAR}` placeholders in the config survive
into the container, where Gatus itself expands them from its process environment. The values reach
that environment through `extraEnv` entries reading optional keys from the `gatus-secret` Secret,
rather than through `envFrom` on the whole Secret: `envFrom` against a missing Secret wedges the pod
in `CreateContainerConfigError`, which would take down the entire monitoring stack over two DNS
checks. An optional key that is absent just leaves the variable empty, so only the two affected
checks fail their conditions and `GatusEndpointDown` pages for them, instead of the deployment
failing to start.

See [KB-065](../troubleshooting/kb/065-gatus-allowlist-dropped-two-groups-silently.md) for the
incident behind the alert's denylist design.
