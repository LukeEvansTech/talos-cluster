# KB-042: gluetun Kubelet Probes Cause a Self-Sustaining Restart Loop

**Status:** Reference. Fixed for `imgur-proxy` in
[#4149](https://github.com/LukeEvansTech/talos-cluster/pull/4149) and
[#4608](https://github.com/LukeEvansTech/talos-cluster/pull/4608). Every gluetun sidecar in this
repository disables the kubelet liveness probe on gluetun's own health server and, since #5681,
enables a startup probe on it (see
[H-20](../../operations/hardening-backlog.md#h-20-three-gluetun-sidecars-had-no-tunnel-gate-at-start-resolved-2026-09-29-5681)).

## Symptom

A pod carrying a gluetun VPN sidecar restarts continuously and never reaches `Ready`. In the
`imgur-proxy` incident this ran for over four days (1352 restarts) with the pod stuck at `OOMKilled`
on every cycle, and the Service had zero ready endpoints for the whole period.

## Cause

A kubelet liveness probe failing on gluetun's health server restarts the _container_, but gluetun
keeps the pod's network namespace across container restarts and re-appends its firewall rules on
every start. A single transient tunnel failure becomes self-sustaining:

```text
health check fails -> kubelet restarts the container -> more accumulated netns state
-> the new start fails too -> health check fails again
```

`imgur-proxy` was the only gluetun sidecar with a liveness probe enabled on the health server at
the time; the repeated restarts against its memory limit produced `OOMKilled`, which looked like a
memory-sizing problem but was not: a fresh pod at the same limit started cleanly and settled at the
same memory usage as every other gluetun sidecar.

## Fix

Disable the kubelet liveness probe on the gluetun sidecar's health server (`liveness.enabled:
false`), matching every other gluetun sidecar in the fleet. gluetun's own health loop restarts the
tunnel in-process, without touching the pod network namespace, when the tunnel degrades after
startup. That loop is `HEALTH_RESTART_VPN`, on by default.

`#4149` and `#4608` credited this recovery to `HEALTH_SERVER_DISABLE_LOOP: off`. gluetun v3.41.3
never reads that variable, so it had no effect either way; the recovery was the default all along.
The variable was removed from every gluetun sidecar on 2026-09-29.

A kubelet **startup** probe is not automatically exempt from this same mechanism: the kubelet
restarts a container whose startup probe fails past its threshold, exactly as it does for liveness,
and gluetun would carry the same accumulated netns state into that restart. Every gluetun
sidecar keeps a startup probe enabled deliberately, gating the app container's own start on it,
and accepts the bounded risk that a genuinely stuck tunnel handshake can trip it within its
`failureThreshold * periodSeconds` window. This differs from the restart loop above in one
respect: a startup probe only runs until it passes once, so the risk window is the pod's actual
startup, not an indefinite post-startup period the way a liveness probe would be.
