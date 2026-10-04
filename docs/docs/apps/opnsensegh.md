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
