# KB-042: gluetun Kubelet Probes Cause a Self-Sustaining Restart Loop

**Status:** Reference. Fixed for `imgur-proxy` in
[#4149](https://github.com/LukeEvansTech/talos-cluster/pull/4149) and corrected in
[#4608](https://github.com/LukeEvansTech/talos-cluster/pull/4608). Every gluetun sidecar in this
repository now disables both kubelet probes and relies on gluetun's own internal health loop
instead.

## Symptom

A pod carrying a gluetun VPN sidecar restarts continuously and never reaches `Ready`. In the
`imgur-proxy` incident this ran for over four days (1352 restarts) with the pod stuck at `OOMKilled`
on every cycle, and the Service had zero ready endpoints for the whole period.

## Cause

A kubelet liveness or readiness probe on gluetun's health server restarts the _container_, but
gluetun keeps the pod's network namespace across container restarts and re-appends its firewall
rules on every start. A single transient tunnel failure becomes self-sustaining:

```text
health check fails -> kubelet restarts the container -> more accumulated netns state
-> the new start fails too -> health check fails again
```

`imgur-proxy` was the only gluetun sidecar with these probes enabled at the time; the repeated
restarts against its memory limit produced `OOMKilled`, which looked like a memory-sizing problem
but was not: a fresh pod at the same limit started cleanly and settled at the same memory usage as
every other gluetun sidecar.

## Fix

Disable both kubelet probes on the gluetun sidecar (`liveness.enabled: false`, and the same for
`readiness`/`startup` where present), matching every other gluetun sidecar in the fleet. Set
`HEALTH_SERVER_DISABLE_LOOP: off` (gluetun's own wording: `off` means _do not_ disable the loop),
so gluetun's internal health-check loop restarts the tunnel in-process, without touching the pod
network namespace, when the tunnel degrades after startup.

`#4149` initially left this internal loop disabled while describing it as the recovery path; `#4608`
found that mismatch (flagged by Codex) and corrected `HEALTH_SERVER_DISABLE_LOOP` to actually enable
the loop it was already being credited for.

A kubelet **startup** probe is a separate, safe case: it gates the main app container from starting
before the tunnel is up, and a failure there blocks pod startup rather than restarting an already
running container, so it does not re-trigger this loop.
