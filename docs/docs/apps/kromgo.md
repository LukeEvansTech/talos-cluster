# Kromgo

## Purpose

`kubernetes/apps/observability/kromgo` generates the cluster's status badges (Talos, Kubernetes,
Flux, node/pod counts, CPU, memory, power, age, uptime, active alerts, and three Gatus-backed
service checks) by evaluating PromQL against `kube-prometheus-stack`.

## Why the chart signature check is scoped per-chart

The `OCIRepository`'s `verify.matchOIDCIdentity.subject` matches only
`home-operations/kromgo/.github/workflows/release.yaml`, not a broader `home-operations/.*`
pattern covering every chart the org publishes. Each `ghcr.io/home-operations/charts/*` chart
releases from its own repository's release workflow, so there is no shared identity to match
against. A regular expression loose enough to cover all of them would let any repository in the
organization sign any chart, which defeats the point of checking the signature at all.

This verification (#3632) started as a canary for the pattern, landing on kromgo first because it
had just missed riding along with an earlier PR. It has since proven out and extended to six more
`home-operations` chart sources (#3631), including `tuppr`, `miroir` and `konflate`.

## Why the connectivity badge uses `min()`/`max()`

The `Internet`, `Alertmanager` and `Grafana` badges wrap their `gatus_results_endpoint_success`
query in `min()` or `max()` rather than reading the series directly. Gatus currently runs a single
replica, so today the wrapper is a no-op, but it means the badges keep working without a query
change if Gatus ever scales past one replica and starts producing a per-replica series.

The `Internet` badge's `connectivity` group checks reachability to Cloudflare, Google and Quad9's
public DNS resolvers.
