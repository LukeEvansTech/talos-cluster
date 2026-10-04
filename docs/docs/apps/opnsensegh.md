# opnsensegh

## Purpose

`kubernetes/apps/observability/opnsensegh` runs a second copy of the API-based
[OPNsense exporter](opnsense-exporter.md) against the firewall at the second site, so its WAN
gateway latency and loss are recorded next to the main firewall's.

The exporter sets its own `OPNSENSE_EXPORTER_INSTANCE_LABEL`, so the existing OPNsense dashboard picks
it up in its instance selector without a dashboard of its own. Gateway RTT and loss come from dpinger
on that firewall, which pings a public anycast address rather than the upstream gateway: upstream
gateways often rate-limit ICMP to themselves and read jitterier than the path beyond them.

## How the scrape reaches the remote site

Pods have no route to the remote LAN. The `opnsensegh-fw` Service is a Tailscale operator egress
proxy whose target is the firewall's LAN address (`GH_OPNSENSE_ADDR` in `cluster-secrets`), reached
through the subnet route that firewall advertises. Two details are load-bearing:

- **The target is the LAN address, not the firewall's tailnet address.** The web GUI listens on
  every interface, but only the LAN address is covered by the anti-lockout pass rule, so the tailnet
  address times out.
- **The proxy needs the `accept-routes` ProxyClass.** An egress proxy does not accept subnet routes
  by default, and without them the LAN address is unroutable from the proxy.

The API key belongs to a dedicated user in a group with the same page privileges as the main
firewall's exporter user. It can read gateway, interface and service state; the config download
endpoint returns 403.

## Alerts

`OPNsenseGhExporterDown` fires after 10 minutes of a failed scrape, a vanished target, or the exporter
reporting `opnsense_up == 0` (it is reachable but its API call to the firewall fails). Because the
scrape crosses the tailnet, check the egress proxy pod before reading it as the remote line being
down.
`OPNsenseGhWanLossHigh` reuses the main firewall's 20% loss threshold. Both are `warning`, since nobody
at the remote site is on call and the history is what this app is for.
