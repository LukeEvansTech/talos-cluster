# KB-069: Veeam backup server BSOD went unnoticed for 12 days

**Status:** Resolved (#4359).

## Symptom

In August 2026, the Veeam B&R server crashed with a BSOD (`CRITICAL_PROCESS_DIED` during a
HotAdd operation) and sat at the crash screen from 2026-08-04 22:00 UTC to 2026-08-16 23:00 UTC,
about 12 days. Nothing paged, and the outage carried real risk: whatever the crash had been
backing up mid-job stayed on a Veeam-managed temporary snapshot for that whole window.

## Cause

No alert watched whether the Veeam server's guest OS was actually running. Prometheus already had
the signal: `vmware_vm_guest_tools_running_status` reports the VMware Tools heartbeat for each VM,
and it sat at `tools_status="toolsNotRunning"` for the whole outage, flipping back to `toolsOk`
only once the server was reset.

## Fix

Added `VeeamServerGuestDown` to `vmware-exporter.rules`:

```text
vmware_vm_guest_tools_running_status{vm_name="${SECRET_VEEAM_VM_NAME}", tools_status!="toolsOk"} == 1
```

for 15 minutes, critical severity. VMware Tools also reports `toolsNotRunning` when a VM is
cleanly powered off (verified live against a stopped VM), so the same alert catches a BSOD, a hung
boot, or an unplanned power-off without separate rules for each. It deliberately has no
`absent()` companion: the exporter itself going down is already covered by `VMwareExporterDown`,
and an `absent()` clause on this series would leave the annotation's `tools_status` label blank
rather than reporting the outage it exists to describe.

## How to recognise fast

A VM's guest-tools heartbeat is a cheap, general signal for "is anything alive in here" that
exists for every VM already running vmware_exporter, independent of whatever workload runs inside
it. Any VM whose uptime matters enough to page on is a candidate for the same pattern, not only
backup servers.

## References

- Fix: #4359.
