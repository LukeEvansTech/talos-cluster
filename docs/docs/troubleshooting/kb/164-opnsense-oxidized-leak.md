# KB-164: OPNsense Config Backup Replaced Oxidized's Plaintext Push

**Status:** Closed. The plaintext push was removed in
[#3775](https://github.com/LukeEvansTech/talos-cluster/pull/3775), and the
`network/opnsense-config-backup` CronJob
([#3777](https://github.com/LukeEvansTech/talos-cluster/pull/3777)) has carried the encrypted
backup since.

## Symptom

GitGuardian flagged five secret-exposure incidents against the GitHub repository that Oxidized
pushed OPNsense's running configuration to. Each flagged commit carried a live ddclient
Cloudflare API token, certificate private keys, and a Tailscale pre-auth key in the clear, because
OPNsense's config export embeds those values with no redaction step.

## Cause

Oxidized's `remove_secret` filter strips sensitive fields from the router and switch config dumps
it also backs up, but no equivalent existed for the OPNsense config export. Oxidized committed
that export verbatim, so every dump after a credential rotation put the new secret back in Git in
the clear.

## Fix

- Rotated the leaked ddclient Cloudflare token (the old value confirmed dead) and confirmed the
  leaked Tailscale pre-auth key was already expired and absent from the tailnet.
- Stopped Oxidized from backing up the OPNsense config
  ([#3775](https://github.com/LukeEvansTech/talos-cluster/pull/3775)).
- Added a dedicated CronJob, `network/opnsense-config-backup`
  ([#3777](https://github.com/LukeEvansTech/talos-cluster/pull/3777)), that pulls `config.xml`
  over the OPNsense API, age-encrypts it before it leaves the pod, and commits only the
  ciphertext to the same repository Oxidized used to write to in the clear.

## References

- [opnsense-config-backup app docs](../../apps/opnsense-config-backup.md)
- [Oxidized app docs](../../apps/oxidized.md)
