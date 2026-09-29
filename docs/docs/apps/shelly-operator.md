# Shelly operator

## Purpose

Reconciles Shelly smart devices from `ShellyDevice` custom resources in the `home` namespace,
alongside the `shelly-prometheus-exporter` metrics scraper. `app/prometheusrule.yaml` ships
fleet-health alerts on the operator's own `shelly_device_*` metrics; power and temperature alerts
live in `shelly-prometheus-exporter`'s separate PrometheusRule.

## Exporter poll interval

`exporterDeviceUpdateInterval` is set to 60s, matching the `shelly-prometheus-exporter`
ServiceMonitor's 60s scrape interval, instead of the chart's 30s default.

That poll accounts for about 98% of all RPC load on the fleet: the exporter makes about 6 calls
per device per cycle, against about 1 per device per `reconcileInterval` from the operator itself.
At the 30s default, half the samples were overwritten before Prometheus read them, so the extra
polling bought no stored resolution and doubled device traffic.

Device firmware 2.0.0 also rate-limits under that load and starts answering `HTTP 429`, which left
the operator unable to read config on roughly a third of the fleet, reporting `InSync=Unknown` for
them (unverified, not the same as healthy). RPC dropped from about 12 to about 6 calls per minute
per device after the change (upstream shelly-operator#54).

Raise the ServiceMonitor scrape interval and this setting together. Raising one alone brings back
either the oversample or the rate limiting.

## Restart-required calibration

A manual sweep before `ShellyDeviceRestartRequired` shipped found four devices pending a restart,
the oldest for four and a half days, with no other record of it. The alert waits `for: 6h`, shorter
than the 24h used for a pending firmware update, because nothing clears a pending restart on its
own.
