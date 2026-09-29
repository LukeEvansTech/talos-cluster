# KB-049: ACME renewal broke silently on two non-cluster Traefik instances

**Status:** Resolved (#3954).

## Symptom

Two Traefik instances outside the cluster issue their own Let's Encrypt certificates over
Cloudflare DNS-01. Nothing in the estate alerted on certificate expiry, so ACME renewal had
been failing on both, and the failure was invisible.

## Cause

Both instances used hand-made, user-owned Cloudflare API tokens carrying an IP allowlist that
no longer matched the home WAN address (`9109: Cannot use the access token from location:
<WAN IP>`), so renewal was dead on both. The two failures behaved very differently, which is
why one was caught and the other would not have been:

- One instance surfaced the problem immediately: a rebuild destroyed its `acme.json` and
  forced an immediate re-issue, which failed at once and fell back to Traefik's default
  self-signed certificate.
- The other surfaced nothing: it still held a cached certificate valid for another six weeks,
  so the broken token was never exercised. It would have failed silently at its own renewal,
  roughly five weeks before anyone would have seen an expiry warning in a browser.

## Fix

Added a dedicated `tls_connect` blackbox module (a bare TLS handshake, not `http_2xx`, since
several probed endpoints are auth-gated and would otherwise read as failed) and a
`lan-tls-cert` Probe over the externally-issued hostnames, plus `CertificateExpiringSoon`
(21d, warning) and `CertificateExpiringCritical` (7d, critical). In-cluster certificates are
excluded, since cert-manager owns and reports on those itself. The broken tokens were also
fixed.

## How to recognise fast

A certificate that renews via a token scoped by source IP is exposed to exactly this failure
mode whenever the allowlisted address changes: a dynamic WAN IP, or a token recreated against
the wrong network. Prefer a token scoped by permission rather than by IP for anything that
renews unattended, or keep the IP allowlist in sync with whatever assigns the address.

## References

- Fix: #3954.
