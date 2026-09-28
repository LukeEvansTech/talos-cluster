# KB-067: Veeam B&R server BSOD sat unnoticed for 12 days

**Status:** Resolved (#4359).

## Symptom

The Veeam Backup & Replication server, a VM outside the cluster, had no monitoring of its own:
nothing paged when it went down, and nothing paged when its backup jobs stopped running.

## Cause

In August 2026 the server crashed mid-job (`CRITICAL_PROCESS_DIED` during a HotAdd operation) and
sat at the crash screen for 12 days before anyone noticed. Both domain controllers ran on Veeam
temp snapshots for the whole outage. No error event was ever produced. The backup schedule simply
stopped advancing, and nothing was watching for that.

## Fix

Three independent layers now cover the server and its jobs:

- `VeeamServerGuestDown` (`vmware-exporter` rules) watches the VM's guest-tools status and fires
  whether the guest crashed or was powered off.
- A blackbox TCP probe on the Veeam REST port, alerted through the generic `LanProbeFailed` rule,
  catches the OS staying up while the Veeam services themselves are dead.
- `kubernetes/apps/observability/veeam-backup` (`docs/docs/apps/veeam-backup.md`) alerts on the
  backup jobs going stale or their metrics disappearing, using metrics the server now publishes
  itself through a textfile collector.

Replayed against the incident, `VeeamServerGuestDown`'s expression fires continuously from the
crash to the reset and stays silent before and after, confirming it would have caught this outage
on the first evaluation.

## How to recognise fast

A backup system that only alerts on job failure has a blind spot: a crashed server, a removed
exporter, or a disabled scheduled task produces no failure event, only silence. Any monitoring
built around "did it succeed" needs a matching "is it still running at all" check, because the two
failure modes look identical to an alert that only watches for errors.

## References

- Fix: #4359.
