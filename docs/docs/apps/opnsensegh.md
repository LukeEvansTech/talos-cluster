# opnsensegh

## Purpose

`kubernetes/apps/observability/opnsensegh` runs a second copy of the API-based
[OPNsense exporter](opnsense-exporter.md) against another OPNsense firewall, under its own
`OPNSENSE_EXPORTER_INSTANCE_LABEL`, so the existing OPNsense dashboard lists it in its instance
selector without a dashboard of its own.

## How the scrape is routed

The target address is `GH_OPNSENSE_ADDR` in `cluster-secrets`, reached through the `opnsensegh-fw`
Tailscale operator egress Service. Two settings are load-bearing:

- **The `accept-routes` ProxyClass.** The target sits behind an advertised subnet route, and an egress
  proxy does not accept subnet routes by default.
- **The target is the firewall's LAN address.** Only that address answers the API; check this first if
  the scrape times out after an address change.

## Alerts

`OPNsenseGhExporterDown` fires after 10 minutes of a failed scrape, a vanished target, or a missing
WAN gateway series (the exporter answers but its API call to the firewall fails). It does not use
`opnsense_up`: exporter v0.0.17 reads this firewall's string `"OK"` system status as down, so that
series sits at 0 while every endpoint works
([upstream #119](https://github.com/AthennaMind/opnsense-exporter/issues/119)). Revisit once a
release fixes it. Check the egress
proxy pod before reading it as the firewall being down. `OPNsenseGhWanLossHigh` reuses the main
firewall's 20% loss threshold. Both are `warning`: the value of this app is the history.

## Speedtest metrics

`kubernetes/apps/observability/opnsenseghspeedtest` exposes the hourly Ookla result that the firewall's
speedtest plugin records. It is a separate app because the exporter above has no speedtest collector and a
second controller in this HelmRelease would share its ServiceMonitor selector.

A [json_exporter](https://github.com/prometheus-community/json_exporter) pod is probed every 5 minutes by a
Prometheus `Probe`, and fetches the plugin's `showrecent` endpoint through the same `opnsensegh-fw` egress
Service. The module config is rendered by the app's ExternalSecret from the `opnsensegh-exporter` item,
because json_exporter can only take basic-auth credentials from its config file.

- **Metrics.** `opnsense_speedtest_download_mbps`, `opnsense_speedtest_upload_mbps` and
  `opnsense_speedtest_latency_ms` are gauges. The API returns them as JSON strings, which json_exporter
  parses as floats.
- **Test time.** json_exporter only reads a numeric epoch for timestamps, and this API returns an ISO date
  string, so the time is the `date` label (UTC, no zone) on `opnsense_speedtest_last_info`.
- **No alerts.** There are no thresholds yet; the Speedtest dashboard shows throughput and latency history.
