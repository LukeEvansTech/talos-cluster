# Deduparr

## Readiness probe calibration

deduparr runs a dedup cycle every `DEDUPARR_SERVICE__INTERVAL` (2m), during which its
single-threaded HTTP server stalls `/health/ready` for about 13s.

The readiness probe originally used a 5s timeout, which timed out on roughly every dedup cycle:
about 51 `Unhealthy` events an hour, never enough consecutive failures to drop the endpoint, so
pure noise. Raising the timeout to 15s outlasts the stall, and the 30s period keeps readiness
from adding load to the single-threaded server.

The liveness probe tolerates the same stall differently: its 10s timeout can still catch the
stall once, but 3 consecutive failures 30s apart (about 90s) are needed before it actually
restarts a hung process.
