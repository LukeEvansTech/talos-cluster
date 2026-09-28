# Shelly operator

Reconciles Shelly smart devices from `ShellyDevice` custom resources, in the `home` namespace,
alongside the `shelly-prometheus-exporter` metrics scraper.

## Purpose

- Manages device discovery, config enforcement and firmware updates for the fleet.
- Ships fleet-health alerts (`shelly_device_*` metrics) in `app/prometheusrule.yaml`; power and
  temperature alerts live in `shelly-prometheus-exporter`'s own PrometheusRule instead.

## Exporter poll interval

`exporterDeviceUpdateInterval` is set to 60s, matching the `shelly-prometheus-exporter`
ServiceMonitor's 60s scrape interval, rather than the chart's 30s default.

That poll is about 98% of all RPC load on the fleet: the exporter makes about 6 calls per device
per cycle against about 1 per device per `reconcileInterval` from the operator itself. At the 30s
default, half the samples were overwritten before Prometheus ever read them, so the extra polling
bought no stored resolution and cost double the device traffic.

Device firmware 2.0.0 also rate-limits and answers `HTTP 429` under that load, which left the
operator unable to read config on roughly a third of the fleet and reporting `InSync=Unknown` for
them, unverified rather than healthy. RPC dropped from about 12 to about 6 calls per minute per
device after the change (upstream shelly-operator#54).

Raise the ServiceMonitor scrape interval and this setting together; raising one alone reintroduces
either the oversample or the rate-limiting.

## Restart-required calibration

A manual sweep before `ShellyDeviceRestartRequired` shipped found four devices pending a restart,
the oldest for four and a half days, with no other record of it. The alert waits `for: 6h`, shorter
than the 24h used for a pending firmware update, because nothing clears a pending restart on its
own.
