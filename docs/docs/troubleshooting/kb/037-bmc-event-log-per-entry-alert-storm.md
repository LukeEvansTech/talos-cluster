# KB-037: BmcEventLogWarning pages once per SEL entry instead of once per fault

**Status:** Resolved (PR #4662, 2026-08-26). PR #4153 (2026-08-09) added the `PWR-0020` exclusion
before this fix landed; PR #5397 (2026-09-25) added the workstation-only exclusion afterward. Both
exclusions are message-label matchers that work independently of the `count by` aggregation below.

## Symptom

`BmcEventLogWarning` fires repeatedly for the same underlying hardware fault, each firing as its
own alert and its own Pushover push. On 2026-08-23 to 08-26 one board went from 3 fired entries to
20 to 79, and by midday on 08-26, 44 of the 62 alerts then standing in Alertmanager were this rule.

## Cause

The BMC's SEL (System Event Log) was logging a failing CR2032 battery: "Battery low" asserting and
deasserting every few minutes as the cell sagged under the sensor threshold and recovered. Each SEL
entry gets its own `id` label from `idrac_events_log_entry`, and the original rule expression used
that metric unaggregated, so Prometheus alerted once per log entry rather than once per fault. A
flapping sensor writes a new entry on every assert/deassert cycle, so 79 entries meant 79 separate
alerts for what was, underneath, one degrading battery.

## Fix

Aggregate with `count by (job, instance, severity, message)` instead of alerting on the raw metric.
Collapsing on those labels keeps every distinct fault visible exactly once and moves the repeat
count into the summary text instead, which is the more useful signal anyway: "logged 79 events"
says more than 79 identical pages. It also stops a re-read of the log, from an exporter restart or
a BMC reset, from re-firing the entire 7-day backlog at once.

Two exclusions apply as `message` matchers on the underlying metric, independent of the
aggregation above and added at different times, both load-bearing for the same reason (an
uninteresting log entry that would otherwise page for the full 7-day window):

- `PWR-0020` ("First AC Power on") is informational and fires once per node on every rack
  power-up. Four fired across a maintenance window and kept paging six days later before this was
  excluded. It's excluded by its exact code rather than the `PWR-` prefix, since that prefix also
  carries real PSU faults.
- `SYS-0067` (a Windows-style blue screen) and `SYS-0069` (an OS graceful shutdown) are excluded
  for the workstation deployment only. The BMC logs the host OS's own lifecycle at Warning
  severity, so every reboot or crash of a desktop machine paged for a full week. On a workstation
  that's the user's own machine telling them something they already saw happen; on a server, the
  same event is exactly the silent failure this alert exists to catch, so servers keep both codes.

## How to recognise fast

A SEL-backed alert repeating for the same `instance` and `message`, with the count climbing over
hours rather than firing once, is a per-entry aggregation problem rather than a genuinely worsening
fault. Check whether the rule's expression aggregates on the metric's own `id` label or has been
collapsed onto the fields that actually identify a fault (host, severity, message).

## References

- PR #4153 (2026-08-09): exclude the informational `PWR-0020` code.
- PR #4662 (2026-08-26): aggregate `BmcEventLogWarning` per fault, not per log entry.
- PR #5397 (2026-09-25): exclude `SYS-0067`/`SYS-0069` for the workstation deployment only.
- [BMC exporter monitoring](../../apps/bmc-exporter.md)
