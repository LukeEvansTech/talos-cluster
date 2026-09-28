# OPNsense Config Backup

A single-container CronJob in the `network` namespace. Daily at 03:30 it pulls OPNsense's full
`config.xml` over the API, encrypts it with [age](https://github.com/FiloSottile/age), and commits
only the ciphertext to a separate GitHub repository.

## Purpose

- Give OPNsense an off-box, version-controlled config backup without ever putting the secrets it
  embeds (ddclient token, certificate private keys, a Tailscale pre-auth key) into Git unencrypted.
- Replaces the plaintext config dump Oxidized used to push to the same repository. See
  [KB-164](../troubleshooting/kb/164-opnsense-oxidized-leak.md) for the leak that prompted the
  change.

## Design decisions

- **`alpine/git` plus a runtime-fetched `age` binary, not a baked image.** `age` is downloaded as a
  pinned release, verified against its published sha256 checksum, and run from a writable
  `emptyDir`. That avoids an `apk` install, so the container keeps a read-only root filesystem and
  runs as a non-root, unprivileged user throughout.
- **`$${VAR}` escaping in the job script.** Flux's `postBuild` substitution runs on every
  HelmRelease value before Kustomize renders it, so a literal `$VAR` the container shell should
  resolve at runtime has to be written as `$${VAR}` or Flux blanks it. `$(...)` command
  substitutions are untouched by that substitution and need no escaping.
- **Age key split between the manifest and 1Password.** The public recipient is a plain HelmRelease
  value, safe to commit; the matching private identity lives only in the `opnsense-config-backup`
  1Password item (`op://Talos/opnsense-config-backup`), so decrypting the backup requires that
  vault.
- **Deploy key scoped to one destination.** The `DEPLOY_KEY` ExternalSecret field is a GitHub
  deploy key with write access to the backup target only, paired with a `KNOWN_HOSTS` entry so the
  job's SSH client pins that host's key instead of trusting it on first connect.
- **Push is a no-op when nothing changed.** The job stages the new ciphertext and only commits and
  pushes if `git diff --cached` finds a difference, so an unchanged OPNsense config produces no
  daily commit noise.
