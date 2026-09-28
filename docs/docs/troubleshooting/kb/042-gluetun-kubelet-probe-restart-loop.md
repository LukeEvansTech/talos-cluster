# KB-042: gluetun Kubelet Probes Cause a Self-Sustaining Restart Loop

**Status:** Reference. Fixed for `imgur-proxy` in
[#4149](https://github.com/LukeEvansTech/talos-cluster/pull/4149) and corrected in
[#4608](https://github.com/LukeEvansTech/talos-cluster/pull/4608). Every gluetun sidecar in this
repository now disables the kubelet liveness probe on gluetun's own health server. Three of them
(`downloads/prowlarr`, `downloads/qbittorrent`, `downloads/sabnzbd`) also disable gluetun's
internal health loop, which trades this restart loop for a different gap; see
[hardening backlog H-20](../../operations/hardening-backlog.md#h-20-three-gluetun-sidecars-have-no-automated-recovery-from-a-post-startup-tunnel-degradation).

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
false`), matching every other gluetun sidecar in the fleet. Set `HEALTH_SERVER_DISABLE_LOOP: off`
(gluetun's own wording: `off` means _do not_ disable the loop), so gluetun's internal health-check
loop restarts the tunnel in-process, without touching the pod network namespace, when the tunnel
degrades after startup.

`#4149` initially left this internal loop disabled while describing it as the recovery path; `#4608`
found that mismatch (flagged by Codex) and corrected `HEALTH_SERVER_DISABLE_LOOP` to actually enable
the loop it was already being credited for.

A kubelet **startup** probe is not automatically exempt from this same mechanism: the kubelet
restarts a container whose startup probe fails past its threshold, exactly as it does for liveness,
and gluetun would carry the same accumulated netns state into that restart. `media/dispatcharr`
keeps a startup probe enabled on gluetun deliberately, gating the app container's own start on it,
and accepts the bounded risk that a genuinely stuck tunnel handshake can trip it within its
`failureThreshold * periodSeconds` window. This differs from the restart loop above in one
respect: a startup probe only runs until it passes once, so the risk window is the pod's actual
startup, not an indefinite post-startup period the way a liveness probe would be.
