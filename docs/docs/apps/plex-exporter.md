# Plex exporter

## Purpose

`kubernetes/apps/observability/plex-exporter` runs `timothystewart6/prometheus-plex-exporter`
against the in-cluster Plex server and alerts on scrape health, server reachability and
transcode-driven host CPU.

## Why a high restart count is expected

The exporter treats a failed Plex websocket read as fatal. It logs "cannot listen to plex server
events" and exits instead of reconnecting itself, so the pod's Kubernetes restart is its reconnect
path. It has no backoff for a Plex server that is not there yet, so while Plex is down, during a
Plex image update for example, the pod hot-loops and its restart count jumps in bursts of 5 to 12
an hour, then sits still for days once Plex is back.

Over the seven days to 2026-07-26 that cost about 25 minutes of scrape gaps, 99.75% availability,
with no restarts after 2026-07-21 (#3845). A restart-count alert was considered and dropped for two
reasons. The bursts already trip `KubePodCrashLooping`, the chart's default crash-loop rule, and
`PlexServerUnreachable` covers the case where the exporter stays up but cannot reach Plex.
app-template also names every container `app`, so a rule written against
`container="plex-exporter"` would match nothing and silently never fire.
