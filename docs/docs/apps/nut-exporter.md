# NUT exporter monitoring

## Purpose

`kubernetes/apps/observability/nut-exporter` runs hon95/prometheus-nut-exporter, which proxies
Prometheus scrapes into the NUT protocol. Each ServiceMonitor endpoint passes the exporter a NUT
server and UPS name through `params.target` and `params.ups`; the Service the ServiceMonitor
selects is the exporter itself, not the UPS being queried.

## Why the NUT server sits off-cluster

Until 2026-07-22 (#3768) NUT ran in-cluster as `network-ups-tools`. It moved to a dedicated
external appliance because the cluster cannot be its own UPS server: during a power event its job
is to be shut down in an orderly way, so whatever sequences that shutdown, and whatever reports
when power returns, has to outlive it. `NUT_SERVER_ADDR` in `cluster-secrets` now points at that
appliance instead of an in-cluster Service.

Reading NUT variables over the protocol is anonymous: `upsd` answers `OK` to `USERNAME`/`PASSWORD`
for a user that does not exist, and only checks credentials at `LOGIN`/`SET`/`INSTCMD`. That is why
this ServiceMonitor carries no credential, confirmed against the appliance with both no
credentials and deliberately wrong ones.

## UPSUnreachable: the 3m fuse, checked against real reboots

`up{job="nut-exporter"} == 0` covers a failed scrape; `absent()` covers the target metric
disappearing entirely rather than resolving to zero. The built-in `TargetDown` fuse is 15m; this
alert uses 3m because the main UPS carries only about 13 minutes of runtime at present load (see
UPSBatteryRuntimeLow), and a 15m wait would burn most of that runtime before anyone learned
monitoring had gone blind.

Before shipping that number, it was checked against 14 days of real reboot history (2026-07-28)
rather than left on trust, because the appliance runs Fedora CoreOS with Zincati auto-updates and
therefore reboots itself:

| Reboot | `up == 0` duration |
| ------ | ------------------ |
| 07-27  | 60s                |
| 07-26  | 60s / 90s          |
| 07-23  | 120s               |
| 07-22  | 105s               |
| 07-21  | 105s / 45s         |

The longest observed gap was 120s, so the 180s (3m) fuse clears every routine reboot with about
50% margin and has not false-fired on one. Raising it to 5m was considered and rejected: it buys
nothing measurable and costs two of the roughly 13 battery minutes before anyone learns UPS
monitoring has gone blind. One case is still unmeasured: no Zincati _upgrade_ reboot, as opposed to
a routine one, has happened since the appliance became the only NUT server. Fedora CoreOS stages
an upgrade before rebooting, so the reboot itself should look the same, but if this alert ever does
false-fire, the fix is to measure the real window, not to guess at a new number.

## UPSBatteryRuntimeLow: why 300s, not 900s

The threshold used to be 900s (15m), and it had to come down. The main UPS delivers only about
780s of runtime on a full battery at roughly 25% load, so a 15m test was already true the instant
a UPS went on battery, making this alert an exact duplicate of UPSOnBattery rather than an
escalation of it. 300s sits below any plausible full-charge runtime for either UPS, so it once
again means what it says.

That 780s figure is measured, not estimated: a live discharge test ran the pack from 100% to 25%
in about 10-11 minutes at 27% load, roughly 40% of what a pack this age should deliver. The pack
was about 17 months old when this was measured in July 2026. The leading theory is a half-seated
battery tray rather than worn cells. If the tray turns out to be the cause and gets reseated,
runtime should roughly double, and 300s would then be too twitchy to be a useful escalation, so
this threshold needs revisiting with fresh numbers rather than being doubled on guesswork.
