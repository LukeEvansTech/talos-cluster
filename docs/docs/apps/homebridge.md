# Homebridge

HomeKit bridge in the `home` namespace, running the `homebridge/homebridge` image.

## Purpose

`kubernetes/apps/home/homebridge` runs Homebridge behind the standard `envoy-internal` route, with
a `homebridge-dummy` startup plugin installed via a ConfigMap-mounted script.

## Why Avahi is disabled

`ENABLE_AVAHI` in `helmrelease.yaml` must be the literal string `"0"`. The image checks this
variable's value only when it is set at all; the image's own baked-in default is
`ENABLE_AVAHI=1`, so merely omitting the variable leaves Avahi enabled.

With Avahi enabled, the container crash-loops. `/etc/s6-overlay/s6-rc.d/avahi/run` starts the
daemon, which fails with "Failed to create runtime directory /run/avahi-daemon/": it chowns that
directory to the `avahi` user and then verifies the ownership, but the container's `CAP_CHOWN`
capability is dropped, so the chown returns `EPERM` and the check fails. Granting `CAP_CHOWN`
clears that failure but exposes a second one: dbus cannot bind `/run/dbus/system_bus_socket` as
root, which an unprivileged user can do, pointing at SELinux rather than at capabilities.

This isn't worth chasing further, because Avahi is not needed here. `config.json` sets
`advertiser: bonjour-hap`, so HomeKit advertisement never used Avahi. HomeKit pairing (HAP) itself
cannot work over this pod's plain Cilium networking regardless, since HAP needs LAN presence that
this pod does not have. `docs/docs/apps/scrypted.md` covers an app in this repository that does
get that LAN presence, through a macvlan attachment.

## Design decisions

- **Runs as root.** The s6-overlay init system needs root to manage `/run` directory ownership,
  which is why `defaultPodOptions.securityContext` sets `runAsNonRoot: false` and UID/GID `0`,
  unlike most apps in this cluster.
- **`SETGID`/`SETUID` capabilities.** dbus-daemon needs these to drop its own privileges after
  starting; without them it fails to do so.
- **`readOnlyRootFilesystem: false`.** Homebridge writes to `/run` and `/tmp` at runtime.
