# Radarr

## Authentication posture

`RADARR__AUTH__REQUIRED` is `Enabled`, not `DisabledForLocalAddresses`. Radarr resolves what counts
as a local address from whatever address it sees on the request, and every request through the
Envoy gateway presents the gateway pod's own address. That address is RFC1918, so the local
exemption applied to all traffic regardless of the real client, not only to browsers on the LAN as
the setting implies.

The value is set explicitly rather than removed, because an unset override falls back to
`config.xml`, which is not tracked in Git. Sonarr and Prowlarr declare the same value for the same
reason.

## Settings that live only in the app

Notification connections are stored in Radarr's database, not in Git. `chaski Download` (id 4)
fires on file import only, without upgrades, since 2026-09-29. The API recipe is in
[Sonarr](sonarr.md).
