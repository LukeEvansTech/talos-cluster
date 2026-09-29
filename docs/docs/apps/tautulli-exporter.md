# Tautulli exporter

## Purpose

`kubernetes/apps/observability/tautulli-exporter` scrapes Tautulli's stats (active streams,
transcodes, LAN/WAN bandwidth) as Prometheus `plex_*` gauges and ships a hand-built Grafana
dashboard for them. The exporter's upstream project, `mm503/tautulli-exporter` on GitHub, ships no
dashboard of its own, and every public "Tautulli" dashboard targets a different exporter's metric
schema, so importing one renders blank against this exporter's series. The deployed image,
`docker.io/mm404/tautulli-exporter`, is that project's published build.

## Metric namespace collision with the Plex exporter

This exporter and the cluster's Plex exporter (the "PPE Plex exporter") both emit metrics under
the `plex_*` prefix, from unrelated schemas. `servicemonitor.yaml` relabels `job` and `instance` on
scrape so Prometheus and the Grafana dashboard can tell the two series sets apart; the dashboard's
PromQL is pinned to `job="tautulli-exporter"` for the same reason.

## Alert design: a v1.0.0 breaking change

Before the exporter's v1.0.0 Go rewrite, `TautulliUnreachable` used
`absent(plex_active_streams_total{job="tautulli-exporter"})`, since the active-streams gauge
disappeared whenever the exporter lost its connection to the Tautulli API.

v1.0.0 changed that behavior: on a failed scrape, only `plex_up` and
`plex_scrape_failures_total` still move. The stream and bandwidth gauges freeze at their last
value instead of disappearing, so `absent()` never fires again. The alert now reads
`plex_up{job="tautulli-exporter"} == 0` instead, which the rewrite's `/ready` endpoint change also
made necessary: `/ready` stopped returning a 503 when Tautulli is unreachable, which is what an
earlier pod-readiness alert had relied on.

## Secrets and API scope

The `ExternalSecret` reuses the existing `tautulli` 1Password item rather than creating one for
this exporter. A read-only Tautulli API key is enough, because the exporter only calls the
`get_activity` endpoint. `TAUTULLI_URL` points at `http://tautulli.media.svc.cluster.local` without
an `/api/v2` suffix; the exporter appends that path itself.

## Dashboard macro protection

The dashboard JSON (`app/dashboards/tautulli.json`) uses Grafana's `${DS_PROMETHEUS}` template
macro to bind its datasource. Flux `postBuild` substitution treats any undefined `${VAR}` as
empty, which would blank that macro, so `app/kustomization.yaml`'s `configMapGenerator` carries
`kustomize.toolkit.fluxcd.io/substitute: disabled` to keep it intact. The `GrafanaDashboard`
resource maps that macro to the cluster's `prometheus` datasource through `spec.datasources`.
