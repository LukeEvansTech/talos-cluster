# Shlink

## Purpose

`kubernetes/apps/default/shlink` runs [Shlink](https://shlink.io/), a URL shortener, behind
`envoy-internal` at `s.${SECRET_DOMAIN}`.

## Design decisions

- `SKIP_INITIAL_GEOLITE_DOWNLOAD` stays `"true"` even though a MaxMind license key is now set:
  the image entrypoint runs under `set -e`, so a MaxMind outage at boot would crash-loop the
  pod. Shlink's visit-time updater fetches the database in the background instead and
  refreshes it every 30 days. It lands on the data PVC (`data/GeoLite2-City.mmdb`) because
  Shlink has no path override; a single ~60 MiB file is an accepted exception to keeping
  caches off backed-up volumes.
