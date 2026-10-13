# Glasshouse TV monitoring

## Purpose

`kubernetes/apps/observability/glasshouse` is a CR-only app: a `ScrapeConfig`, the
`ExternalSecret` holding its token, and one `PrometheusRule`, with no HelmRelease. [Glasshouse](https://github.com/rorygallagher2024/glasshouse)
runs as root on each rooted LG webOS TV and serves panel wear, SoC temperature, HDMI signal and
Magic Remote battery metrics at `/api/prometheus/metrics`. Nothing runs in the cluster; it only
scrapes.

## Configuration

- **Token.** The dashboard API has no authentication by default, so every TV is deployed with the
  same `token`, kept in the `glasshouse` 1Password item as `GLASSHOUSE_TOKEN`. The scrape sends it
  as a Bearer header; without it the endpoint answers 401.
- **Targets.** Each TV's address comes from a `SECRET_GLASSHOUSE_TV<n>_HOST` key in
  `cluster-secrets`, so no address is committed. Add a TV by adding the key to 1Password, then a
  target with its own `tv` label.
- **Interval.** 15 minutes, so the TVs can sleep. Some TVs return from suspend to Active
  Standby on any inbound packet, so a frequent scrape keeps them awake all night. Samples older than the 5-minute lookback show as gaps in graphs, which is fine
  for panel and thermal trends.
- **TargetDown.** A TV switched off at the wall stops answering, which is not an outage. Its
  target carries `device_class: tv`, which the [kube-prometheus-stack](kube-prometheus-stack.md)
  `TargetDown` patch excludes the same way it excludes idle printers.

## Prerequisite on each TV

The exporter is **off** by default upstream. Each TV's `/var/lib/tvweb/config.json` needs
`"prometheus": { "enabled": true }` and the shared `token`, or the target sits at `up == 0`.
Because `TargetDown` excludes TVs, that would be silent, so `GlasshouseNeverScraped` fires
instead when a TV has not answered a single scrape in 7 days. A TV that is merely switched off at
night still answers within the week.

## MQTT

Glasshouse can also publish to the `home/mosquitto` broker for Home Assistant. Each TV logs in as
its own broker user, whose name is also its topic prefix and discovery device ID, so the broker
ACL confines it to its own topics and entities. The user's password is
`<USER>_MQTT_PASSWORD` in the same 1Password item.
