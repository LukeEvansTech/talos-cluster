# Blackbox exporter (LAN)

## Purpose

`kubernetes/apps/observability/blackbox-exporter/lan` probes devices and services that have no
Prometheus exporter of their own: BMC/firewall reachability over ICMP, the wireless access points,
an NFS port check, and TLS certificate expiry on both non-cluster Traefik instances and BMC
management endpoints.

## Why the access points get their own probe and DNS names

The access point probe (`ap_probe`) exists as a second, independent signal alongside the access
switch's per-port PoE reading (`snmp-exporter`): PoE says an access point is drawing power, this
probe says it is actually answering on the network. A hung access point still draws its normal
power, so neither check can stand in for the other.

The targets are DNS names rather than addresses, served by Unbound from reserved DHCP leases
(`network-ops` `ansible/vars/{dhcp,dns}.yml`). Before those names existed, the access points had no
probe at all, because a dynamic address is nothing to point one at. The wireless controller role
floats between the access points after a reboot, so no single one of them is durably "the
controller"; the UPS management card's own probe target resolves the same way, through an Unbound
host override shadowing a stale public DNS record.

## Why the BMC certificate probe is separate from the Traefik one

`lan-tls-cert-device` exists because a Certwarden certificate deploy Job can "succeed" while the
device keeps serving its old certificate until reboot. One BMC served an expired certificate for
3.5 months with nothing anywhere to say so; probing what the socket actually serves catches that,
weeks before expiry.

It has to be a separate `Probe` object, and use `tls_connect_noverify` rather than the Traefik
targets' `tls_connect`, because these BMCs serve a certificate chain with no intermediate. That is
a firmware constraint, not a choice of deploy tool: verified live on one board's firmware, the
vendor's own certificate-upload API rejects any multi-cert PEM, and the standard replace-certificate
call only pairs with its own CSR-generation flow. A verifying probe would therefore score every one
of these targets as failed regardless of certificate validity.

Both probes share one `jobName` (`tls_cert`) so dashboards and the expiry alerts treat the Traefik
and BMC certificates as one fleet.

## The scrape-timeout clamp that halved a 30s budget

`tls_connect_noverify`'s module `timeout: 30s` is not what actually bounds the probe. Blackbox
clamps its own module timeout to the scrape timeout Prometheus sends in the
`X-Prometheus-Scrape-Timeout-Seconds` header, minus a 0.5 second offset. Against the chart's 10s
default scrape timeout that clamp is 9.5s, so the 30s module setting never applied, and the storage
BMC failed about 83% of its scrapes for a week while serving its certificate perfectly. Measured
handshakes on that BMC ranged 1-3s warm, but 6.6s, 15s and 20s on the first connection after an idle
gap, which is every scrape at a 5-minute interval.

The fix is the `lan-tls-cert-device` Probe's own `scrapeTimeout: 35s`, which raises the value
blackbox actually clamps against. Raise the module timeout and this `scrapeTimeout` together, or
neither: raising only the module timeout repeats the exact failure this one fixed.
