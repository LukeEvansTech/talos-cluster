# Unimus

## Purpose

Unimus backs up network device configurations, in the `observability` namespace
(`kubernetes/apps/observability/unimus`). A daily `seed` CronJob (04:00) creates the device
credentials and devices from 1Password through the Unimus API; the nightly discovery and backup
runs at 03:00.

## Logins and secrets

- **Web login**: user `admin`, from the `unimus` item in the `Talos` 1Password vault. It is the only
  Unimus user. A login with any other username, such as a stale personal 1Password entry, fails.
- **Database encryption key**: Unimus reads it from `database.encryption.key` in
  `/etc/unimus/unimus.properties` on its own volume, not from the ExternalSecret. The
  `UNIMUS_DB_ENCRYPTION_KEY` field in the `unimus` item is the off-cluster copy; on 2026-11-09 the
  two matched. Without it, a restored database cannot decrypt the stored credentials.
- **API token**: `UNIMUS_API_TOKEN`, used only by the seed job. Unimus shows the full token in the
  User management API tokens section, so keep that section collapsed while screen-sharing or
  taking screenshots.

## Credential bindings

The API cannot bind a credential to a device (see the `seed.py` docstring in `app/configmap.yaml`),
so bindings are made by hand: edit the device, then **Bind…** next to Credentials and, for devices
that need one, next to Enable password. All seven devices were bound on 2026-11-09.

The seed job recognises its credentials by a fingerprint keyed by the API token, so **rotating
`UNIMUS_API_TOKEN` makes the next seed run recreate every credential, unbound**. After a rotation,
re-bind every device and run a discovery. Changing a single credential's secret does the same for
that credential's devices.

Unbound credentials still work, because discovery tries the whole pool, but each attempt that
fails is an SSH authentication failure on the device. Wazuh downgrades the resulting alert with
rule 100110 ([Wazuh](wazuh.md#local-rules)).
