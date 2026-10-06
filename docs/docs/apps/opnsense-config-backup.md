# OPNsense Config Backup

Two single-container CronJobs in the `network` namespace, one per firewall. Daily (03:30 for the
main firewall, 03:45 for the second, `opnsensegh`) each pulls OPNsense's full `config.xml` over the
API, encrypts it with [age](https://github.com/FiloSottile/age), and commits only the ciphertext
(`opnsense.xml.age`, `opnsensegh.xml.age`) to a separate GitHub repository.

## Purpose

- Give OPNsense an off-box, version-controlled config backup without ever putting the secrets it
  embeds (ddclient token, certificate private keys, a Tailscale pre-auth key) into Git unencrypted.
- Replaces the plaintext config dump Oxidized used to push to the same repository. See
  [KB-070](../troubleshooting/kb/070-firewall-backup-leaked-secrets.md) for the leak that prompted the
  change.

## Design decisions

- **`alpine/git` plus a runtime-fetched `age` binary, not a baked image.** `age` is downloaded as a
  pinned release, verified against its published sha256 checksum, and run from a writable
  `emptyDir`. That avoids an `apk` install, so the container keeps a read-only root filesystem and
  runs as a non-root, unprivileged user throughout.
- **`$${VAR}` escaping in the job script.** Flux's `postBuild` substitution runs on the Kustomize
  output, before helm-controller renders the chart, so a literal `$VAR` the container shell should
  resolve at runtime has to be written as `$${VAR}` or Flux blanks it. `$(...)` command
  substitutions are untouched by that substitution and need no escaping.
- **Age key split between the manifest and 1Password.** The public recipient is a plain HelmRelease
  value, safe to commit; the matching private identity lives only in the `opnsense-config-backup`
  1Password item (`op://Talos/opnsense-config-backup`), so decrypting the backup requires that
  vault.
- **Deploy key scoped to one destination.** The `DEPLOY_KEY` ExternalSecret field is a GitHub
  deploy key with write access to the backup target only, paired with a `KNOWN_HOSTS` entry so the
  job's SSH client pins that host's key instead of trusting it on first connect.
- **One controller per firewall, one shared script.** The two jobs are controllers in the same
  HelmRelease; the second reuses the first's container through a YAML merge key and differs only in
  `BACKUP_NAME` and `BACKUP_FILE`. Separate jobs keep one firewall's outage from failing the other's
  backup, and the CronJob name tells an alert which one broke. Pods reach the second firewall directly
  over the site-to-site tunnel, so no proxy is involved.
- **Two secrets, one source of push credentials.** The second job's ExternalSecret reads the API
  credentials (`OPNSENSE_HOST`, `OPNSENSE_API_KEY`, `OPNSENSE_API_SECRET`) from the
  `opnsensegh-config-backup` item and the `DEPLOY_KEY` and `KNOWN_HOSTS` fields from
  `opnsense-config-backup`. Its absence only affects the second job.
- **No push race.** The schedules are 15 minutes apart, and a rejected push is retried after
  `git pull --rebase`, so the jobs cannot lose each other's commits.
- **Absence-of-success alert.** `OPNsenseConfigBackupStale` fires when either CronJob has not
  succeeded for about 26 hours. Until the second item exists the second job fails and this alert
  fires for it.
