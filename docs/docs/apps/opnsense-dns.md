# opnsense-dns

## Purpose

`kubernetes/apps/network/opnsense-dns` is the internal instance of external-dns. It writes host
overrides into OPNsense's Unbound resolver through a purpose-built webhook provider instead of a
stock external-dns provider. See [Split DNS](../architecture/split-dns.md) for how it fits beside
the public `cloudflare-dns` instance, how record ownership works, and the alert list.

## TLS verification needs the firewall's FQDN, not its IP

`OPNSENSE_HOST` must stay the firewall's FQDN. The firewall serves a publicly trusted certificate
for that FQDN, so the provider's default TLS verification works, but the certificate carries no IP
subject alternative name. Pointing the host at the bare IP instead fails every call
([#5204](https://github.com/LukeEvansTech/talos-cluster/pull/5204)).

## The apply-timeout budget

`deploymentStrategy.type: Recreate`, `terminationGracePeriodSeconds: 180` and
`--webhook-provider-write-timeout=180s` all need to outlast a single apply. The provider's own
`OPNSENSE_APPLY_TIMEOUT` and `OPNSENSE_RECONFIGURE_TIMEOUT` budgets sum to about 165 seconds, so
the 180-second values give roughly 15 seconds of margin above the slowest possible apply before the
controller's write call or the pod's grace period would cut it off
([#5159](https://github.com/LukeEvansTech/talos-cluster/pull/5159)).

## Pinning the annotation prefix ahead of external-dns 1.22.0

`annotationPrefix` is pinned to the alpha form (`external-dns.alpha.kubernetes.io/`) instead of
being left at the chart default. external-dns 1.22.0 changes that default to the GA prefix
(`external-dns.kubernetes.io/`), and every annotation in this repository still uses the alpha form.
Under `policy: sync`, an unpinned bump to 1.22.0 would read zero annotations and delete every
record opnsense-dns owns ([#5108](https://github.com/LukeEvansTech/talos-cluster/pull/5108)). Drop
the pin only once the annotations are rewritten to the GA prefix.

## References

- [Split DNS: opnsense-dns configuration and alerts](../architecture/split-dns.md#opnsense-dns-internal)
- [KB-033: OPNsense record exists but is unowned](../troubleshooting/kb/033-opnsense-record-exists-but-is-unowned.md)
- [KB-034: OPNsense delete blocked by a hand-made alias](../troubleshooting/kb/034-opnsense-delete-blocked-by-alias.md)
