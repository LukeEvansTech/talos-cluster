# KB-070: Oxidized's firewall backup leaked plaintext secrets into Git

**Status:** Mitigated (#3775, #3777). No remediation is recorded for the leaked certificate private
keys, which remain in the backup repository's history.

## Symptom

GitGuardian flagged five incidents (32670994, 32670995, 33916995, 33991644, 34105179) against the
`network-configs` repository that Oxidized pushes device configs to.

## Cause

Oxidized backed up the firewall the same way as the access switches: a full config dump over SSH,
committed as-is. The firewall's `config.xml` export carries the ddclient Cloudflare token,
certificate private keys, and the Tailscale pre-auth key in plain text, and every one of those
values landed in Git on each poll.

The switches' exports hide sensitive values, so backing those up through Oxidized was safe from the
start.

## Fix

Removed the firewall from Oxidized's `router.db` (#3775). Its config is now backed up by the
`opnsense-config-backup` CronJob in the `network` namespace (#3777), which pulls `config.xml` over
the firewall's API once a day, encrypts it with `age`, and commits only the ciphertext to the same
repository. Restoring it needs the separately stored `age` identity.

The leaked ddclient token was rolled and the old value confirmed dead. The leaked Tailscale pre-auth
key was confirmed expired and absent from the tailnet. Neither the commit nor the PR records any
action on the certificate private keys, so treat those certificates as exposed until they are
reissued.

## How to recognise fast

Before pointing a config-backup tool at a new device, check whether its export format embeds
credentials in plain text rather than referencing them. One vendor's redacted export format says
nothing about the next vendor's.

## References

- Oxidized change: #3775.
- Encrypted replacement: #3777.
