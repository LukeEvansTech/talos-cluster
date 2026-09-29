# BMC exporter monitoring

## Purpose

`kubernetes/apps/observability/bmc-exporter` runs `idrac_exporter` against a fleet of Supermicro
BMCs over Redfish, plus a second, single-target deployment for a separate workstation BMC.
Together they feed two Grafana dashboards and the `PrometheusRule` alerts in this directory.

## Naming and metric prefix

The HelmRelease is named `bmc-exporter` for what it does here, but the chart, image and metric
prefix stay upstream's `idrac`. The exporter is vendor-neutral Redfish, explicitly tested against
Supermicro, and keeping the default `idrac_` prefix means upstream's dashboards and docs still
apply to this deployment. The prefix is settable, but changing it later would orphan every stored
series, so it stays as-is on purpose rather than by omission.

## What the boards actually return

Measured against the live fleet rather than assumed from the Redfish schema:

- temperatures, fan RPM and health, voltages, PSU health/watts/input voltage, per-DIMM health,
  per-CPU health, BMC manager health, system power state
- no storage metrics on either board generation. Supermicro doesn't populate that part of the
  Redfish tree, so drive health stays with smartctl-exporter
- a powered-off host drops all of its sensor series and reports only `idrac_system_power_on 0`,
  which is what the alert rules key off

## Why the workstation gets its own deployment

The workstation is a desk machine, powered off most of the time, so it needs to be exempt from
`BmcHostPoweredOff` while staying covered by every other rule. Excluding one host from one alert
needs something in PromQL to match on, and none of the obvious options work:

- `instance` is the target address, and this BMC has no DNS record, so matching on it would put a
  LAN address in this public repository
- the `model` label doesn't discriminate: this board and the storage node both report the generic
  "Super Server"
- the ScrapeConfig relabelings could synthesise a label, but only by matching the address, which
  is the same leak

Giving it its own deployment gives it its own `job` label instead, a plain string that discloses
nothing. The rules then use `job=~"bmc-exporter.*"` for hardware health and `job="bmc-exporter"`
exactly for power state, so the exact match excludes the workstation. The cost is one more 64Mi
pod. If an internal DNS record is ever created for this BMC, the tidier form is to fold it back
into the main config and select on the hostname instead.

`BmcHostPoweredOff` also gates on the host having been on within the last 24h, so an unexpected
power-off still pages within 15m and keeps paging for a day, then the alert self-silences once the
host reads as deliberately parked. That is what stops a retired sled or a cold spare from paging
forever, without keeping a hostname list in a public repository, which would only drift as boxes
get repurposed.

## Scrape timing

Targets come from the exporter's own `/discover` endpoint, which lists exactly the hosts in the
mounted config, so adding a BMC to the `ExternalSecret` is the only change needed to add a scrape
target.

The scrape interval is 60s with a 55s timeout, not the usual 30s, because a warm scrape measured
about 7.5s on the H13 boards and about 13s on the X12, and a cold scrape right after a BMC restart
measured about 23s on the X12. The exporter's own `timeout: 45` config setting sits below that 55s
ScrapeConfig timeout and above the exporter's 10s default, which was aborting mid-collection on
this hardware.

## PSU power reading vs system power draw

The fleet dashboard's power panels sum `idrac_power_control_consumed_watts`, the system-level
reading, rather than `idrac_power_supply_output_watts` per PSU. On the H13 boards the two disagree
by an order of magnitude: a node drawing about 95W at the system level reports around 450W per PSU.
The PSU figure is shown on its own panel, labelled as reported, and isn't meant to be read as
consumption.

## Temperature thresholds

Thresholds are split by sensor class, taken from `max by (name)` across the whole running fleet
rather than one sampled host, because a single fleet-wide number can't fit the spread:

| Sensor class    | Peak measured |
| --------------- | ------------- |
| CPU Temp        | 77C           |
| AOC_NIC1        | 68C           |
| MLP_NIC         | 66C           |
| GPU0            | 63C           |
| M2_SSD          | 53C           |
| Everything else | 49C or below  |

A single 80C threshold for every sensor would sit only 3C off a normal CPU reading while letting a
NIC reach 79C unnoticed. So CPU packages get their own band (alerting from 90C, critical above
95C), and everything else gets a wider one (alerting from 80C, critical above 90C), which leaves
about 12C of headroom over the hottest non-CPU sensor instead of 3C.

The CPU band's regular expression is `CPU[0-9]* Temp` rather than a bare `CPU` prefix, so it
matches a second socket (`CPU1 Temp`) without also catching the much cooler `CPU_VRM0 Temp` or
`SOC_VRM Temp` rails.

## SEL event log

Supermicro SEL entries surface as `idrac_events_log_entry`, bounded to `severity: warning` and
`maxage: 7d` in the exporter config, since `message` is a label and an unfiltered log would be a
cardinality risk. The `BmcEventLogWarning` rule's aggregation choice and two exclusion codes have
their own history in [KB-037](../troubleshooting/kb/037-bmc-event-log-per-entry-alert-storm.md).
