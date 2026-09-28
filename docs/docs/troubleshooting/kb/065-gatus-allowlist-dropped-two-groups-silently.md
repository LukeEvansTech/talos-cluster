# KB-065: Gatus alerting was off for months, then an allowlist dropped two more groups

**Status:** Resolved (#3372).

## Symptom

No Gatus endpoint going down had ever paged anyone. Checked against live Prometheus: 343 alert
rules were loaded across 67 groups, and none of them were Gatus's.

## Cause

`GatusEndpointDown` had been scaffolded commented-out in the app's `kustomization.yaml` months
earlier and never enabled. Once enabled, its expression matched groups by an allowlist
(`group=~"internal|external"`), which silently excluded two groups Gatus had auto-discovered since:
`security` (the anubis check) and `default` (the shlink check). Either could fail forever without
ever paging.

## Fix

The rule was enabled, and its expression changed from that allowlist to a denylist
(`group!="connectivity"`), so any future auto-discovered group pages by default. `connectivity`
(public DNS ICMP checks) stays excluded on purpose, since those flap on a single network blip.

## How to recognise fast

An allowlist on a label a tool assigns automatically, such as Gatus's endpoint-discovery group,
silently excludes anything discovered after the allowlist was written. Prefer a denylist for a
small, named set of exceptions over an allowlist for a set that keeps growing on its own.

## References

- Fix: #3372.
