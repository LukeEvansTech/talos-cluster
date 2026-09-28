# KB-070: Oxidized's OPNsense backup leaked plaintext secrets into Git

**Status:** Resolved (#3775).

## Symptom

GitGuardian flagged five incidents (32670994, 32670995, 33916995, 33991644, 34105179) against the
`network-configs` repository that Oxidized pushes device configs to.

## Cause

Oxidized backed up OPNsense the same way as the MikroTik switches: a full config dump over SSH,
committed as-is. OPNsense's `config.xml` export carries the ddclient Cloudflare token, certificate
private keys, and the Tailscale pre-auth key in plain text, and every one of those values landed in
Git on each poll.

RouterOS exports do not have this problem: the MikroTik switches already hide sensitive values in
their own export format, so backing those up through Oxidized was safe from the start.

## Fix

Removed OPNsense from Oxidized's `router.db` entirely rather than trying to filter the dump.
OPNsense now uses its own encrypted backup mechanism instead. The leaked ddclient token was rolled
and the old value confirmed dead; the leaked Tailscale pre-auth key was confirmed expired and
absent from the tailnet, so neither leak was exploitable by the time it was found.

## How to recognise fast

Before pointing a config-backup tool at a new device, check whether its export format embeds
credentials in plain text rather than referencing them. One vendor's redacted export format says
nothing about the next vendor's.

## References

- Fix: #3775.
