# Networking

## CNI — Cilium

Pod networking is Cilium. Pod and service CIDRs are the standard cluster-internal ranges and are not
sensitive. Network policy is enforced with `CiliumNetworkPolicy` (mind the gotchas around empty
rules, `ingressDeny`, and egress-to-world).

## Ingress — Envoy Gateway (Gateway API)

Ingress is Envoy Gateway using the Gateway API (`HTTPRoute`), not classic Ingress. Two listeners
exist, addressed by role rather than by IP here:

- an **internal** listener (LAN-only), and
- an **external** listener (reachable via the Cloudflare tunnel).

Most apps declare an inline `route:` in their HelmRelease values targeting the appropriate listener
in the `network` namespace. Hosts are `${APP}.${SECRET_DOMAIN}` (external) and
`${APP}.${SECRET_INTERNAL_DOMAIN}` (internal). "Available under `${SECRET_DOMAIN}`" for a home app
usually means internal DNS on the internal listener — not public exposure.

## DNS

- **Internal**: external-dns publishes records to the on-prem firewall's resolver.
- **External**: Cloudflare DNS plus a `cloudflared` tunnel for the handful of publicly-exposed apps.

See [Split DNS](split-dns.md) for how the internal and external views are kept separate, and the
[Envoy Gateway migration](../migrations/envoy-gateway.md) for how ingress reached its current shape.

## TLS

cert-manager issues certificates via Let's Encrypt ACME; the external domain's certificates are
visible in certificate-transparency logs (which is why the external domain itself is not treated as
secret, while internal hostnames are kept out of Git).
