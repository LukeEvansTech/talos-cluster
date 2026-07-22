# Networking

## CNI — Cilium

Pod networking is Cilium. Pod and service CIDRs are the standard cluster-internal ranges and are not
sensitive. Network policy is enforced with `CiliumNetworkPolicy` (mind the gotchas around empty
rules, `ingressDeny`, and egress-to-world).

## Ingress — Envoy Gateway (Gateway API)

Ingress is Envoy Gateway using the Gateway API (`HTTPRoute`), not classic Ingress. Two Gateways
exist in the `network` namespace, addressed by role rather than by IP here:

- **`envoy-internal`** (LAN-only), and
- **`envoy-external`** (reachable via the Cloudflare tunnel).

Most apps declare an inline `route:` in their HelmRelease values targeting one of the two Gateways.
Every route uses a single hostname, `${APP}.${SECRET_DOMAIN}`, whichever Gateway it attaches to:
internal-only vs public exposure is decided by the Gateway, not by the domain. Routes do not carry
`${SECRET_INTERNAL_DOMAIN}` aliases: an alias resolves to the same Gateway as the primary hostname,
so it buys no extra restriction, and each one costs an OPNsense host-override record against a hard
ceiling (see [Split DNS](split-dns.md)). "Available under `${SECRET_DOMAIN}`" for a home app
usually means internal DNS on `envoy-internal`, not public exposure.

## DNS

- **Internal**: external-dns publishes records to the on-prem firewall's resolver.
- **External**: Cloudflare DNS plus a `cloudflared` tunnel for the handful of publicly-exposed apps.

See [Split DNS](split-dns.md) for how the internal and external views are kept separate, and the
[Envoy Gateway migration](../migrations/envoy-gateway.md) for how ingress reached its current shape.

## TLS

cert-manager issues a single wildcard certificate (`*.${SECRET_DOMAIN}`) via Let's Encrypt ACME,
and both Gateways serve it. The wildcard entry is visible in certificate-transparency logs (which is
why the domain itself is not treated as secret), but individual app subdomains are not enumerated
there, and internal hostnames are kept out of Git.
