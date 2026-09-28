# OPNsense exporter

## Purpose

`kubernetes/apps/observability/opnsense-exporter` scrapes the firewall two ways: the
`opnsense-exporter` API-based exporter for gateway, service and IPsec state, and a plain
node-exporter `ScrapeConfig` for kernel-level CPU/memory/ZFS/disk/netdev the API cannot see. The
node-exporter path was added after a WAN-wedge outage had to be reconstructed entirely from
cluster-side probes, because the box exposed no OS-level metrics at the time.

## Why the WAN-traffic-frozen alert's `for` stays short

`OPNsenseWanTrafficFrozen` fires on a `rate(...) == 0` over a 5-minute window with `for: 1m`, not a
longer `for` on top of that window. The 5-minute `rate()` window is itself the debounce: the
ServiceMonitor scrapes every 60s, and `rate()` needs at least two samples in range, so a 1-minute
window would usually see only one and could never sustain long enough to fire at all. Stacking
another 5 minutes of `for` on top of that window would double the effective detection time to
about 10 minutes for no benefit, since the rate window has already absorbed the debounce.

The alert exists because of a specific outage signature: both WAN interface counters froze to
exactly zero for 15 minutes while the link stayed up and nothing else logged a fault. dpinger's own
monitor traffic rides the same interface, so a healthy WAN can never legitimately read zero bytes
in both directions for 5 minutes; any observed instance of that is the interface itself, not
absence of traffic to send.

## Why the backup WAN tier alert is gated on state, not a time window

`OPNsenseBackupWanTierDown` fires on sustained 100% loss on a backup WAN tier, gated on the
exporter's own `opnsense_gateways_status != 6` ("offline forced", OPNsense's state for a gateway an
admin has explicitly forced down) rather than on any time-based window. A tier being wired up or
deliberately retired stays silent by forcing it down in OPNsense; anything not force-down alerts on
sustained loss with no expiry.

The earlier version of this gate used `min_over_time(...[24h]) < 100` to both arm on first success
and disarm after 24 hours, using the same rolling window for both jobs. Once a real outage ran past
24 hours, the window filled entirely with 100%-loss samples and the alert silently resolved itself
while the failure was still active, indistinguishable from a deliberate decommission. That
self-silenced during the one case the alert exists to catch, which is why the gate moved to an
explicit administrative state instead of an implicit time-based one.

## IPsec tunnel monitoring

`OPNsenseIPsecTunnelDown` exists because both ends of the site-to-site tunnel pin their identity to
a literal WAN IP address, so an upstream address change leaves the tunnel down until both sides are
updated together, a manual, two-sided fix rather than something that resolves on its own. Before
this alert, `phase1_status` had been scraped all along with no rule on it, so the tunnel could sit
down indefinitely with every dashboard reading green.

The `absent()` half of the expression is load-bearing, not decoration. If the connection is
removed, renamed, or simply stops being returned by the API, its series vanishes entirely, and a
bare `== 0` comparison can never fire on an empty vector. That is the same blind spot that once left
an unrelated exporter's outage unmonitored for eight days; this alert was written to not repeat it,
and was verified live against a description that does not exist to confirm it actually fires.

`OPNsenseIPsecChildSAMissing` catches the case one layer down: phase 1 (IKE) can be up while phase 2
(the actual traffic path) has no child security association, which the tunnel-down alert above
cannot see. There is no `phase2_status` metric, and the byte counters are not a usable substitute,
since a healthy, idle tunnel legitimately reads zero bytes transferred. Existence of the child SA is
therefore the only usable signal. The 15-minute `for` clears a normal phase 2 rekey window (a
lifetime of roughly 2300 seconds, rekeying around 1500 seconds in) without flapping on it.
