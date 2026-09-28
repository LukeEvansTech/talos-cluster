# SNMP exporter

## Purpose

`kubernetes/apps/observability/snmp-exporter` polls SNMP devices that have no native Prometheus
exporter: the core switch, the two MikroTik switches, the APC rack PDUs, and the Brother printer
fleet. Each device family gets its own scrape target and, where the device needs one, a custom
`snmp_exporter` module in a dedicated ConfigMap.

## Why the core switch is two scrape targets

`if_mib` (port counters and status) and `entity_sensor` (temperature, fan, PSU, optics) are
scraped as separate targets rather than one target running both modules. A scrape is only as
healthy as its slowest module. Measured against the switch on 2026-08-27, 8 walks each through the
exporter's own `/snmp` endpoint:

| Walk                  | Success rate | Avg time | Max time |
| --------------------- | ------------ | -------- | -------- |
| `if_mib` alone        | 8/8          | 0.3s     | 1.9s     |
| `entity_sensor` alone | 5/8          | 11.3s    | 20.1s    |
| both together         | 4/8          | 11.0s    | 20.1s    |

Combined, that marked the whole target down about 29% of the day, so `up` read "core switch
unreachable" while its interface data was in fact fast and complete, and the critical
`SnmpTargetDown` alert was one slightly longer stall away from paging on it. Split, `if_mib` stays
a trustworthy reachability signal and a slow sensor walk can no longer forge an outage. The same
split, for the same reason, is why the MikroTik switches are each two targets (health/PoE vs
interfaces); see [device & infrastructure monitoring](../operations/device-monitoring.md) for that
measurement.

## MikroTik module: what is not hardcoded

`mtxrHealthTable` is self-describing (column `.2` is the sensor name, `.3` the reading, `.4` a
unit code), and which sensors exist differs by model, so the set of sensors is not hardcoded in
the ConfigMap. Observed unit codes are `1=celsius`, `2=rpm`, `6=state` (boolean). Codes `3`/`4`/`5`
appear on voltage/current/power rows respectively, but the exact scaling factor has not been
confirmed against the device CLI, so nothing in the module or its alerts assumes one.

`mtxrPOETable` carries the port's own name in column `.2`, so the per-port device label is looked
up from the switch at scrape time rather than written into this public repository.

## PoE-based device monitoring: alert design

Devices with no exporter, agent or probe of their own (access points, a Zigbee coordinator) are
watched through the switch's own PoE meter instead; see [device & infrastructure
monitoring](../operations/device-monitoring.md) for why that signal is reliable here. Two
implementation choices in `MikrotikPoeDeviceLostPower`:

- It compares `mtxrPOEPower` against zero, not the `mtxrPOEStatus` enum. The status enum's meaning
  was inferred from observation, not from a MIB the exporter holds, so an alert built on it would
  encode a guess. Zero-versus-nonzero needs neither the enum nor the unconfirmed power scaling
  factor.
- The lookback term is `max_over_time`, not `min_over_time`. The rule has to answer "was this port
  ever powered recently", and `min_over_time` answers "was it powered at every sample", which
  breaks the rule two ways: a single zero during the reboot window the `for: 15m` exists to
  tolerate would blank the evidence for the next 6h and suppress a real outage in that period, and
  during a sustained outage the first zero enters the 6h offset window after about 30 minutes, so
  the alert would resolve itself roughly 15 minutes after firing while the device was still dead.
  With `for: 15m` and no further activity, the alert self-resolves about 6.5 hours after the device
  is unplugged.

## Scrape-health alerts are a failure rate, not `up == 0`

`OnyxSensorScrapeDegraded` and `MikrotikInterfaceScrapeDegraded` both fire on `1 -
avg_over_time(up[1h]) > 0.5` rather than `up == 0` with a long `for:`. The walks they cover fail
intermittently in short bursts, mostly under 5 minutes, which never trips a `for:` clause. Before
this rule shape existed, the equivalent combined target flapped about 130 times a day while going
completely unalerted. Writing the expression as `1 - avg_over_time(...)` rather than comparing the
success rate directly means `$value` in the alert is the failure fraction the summary quotes;
`avg_over_time(up)` alone is the success fraction, so the direct form would understate how often
the walk is actually failing.
