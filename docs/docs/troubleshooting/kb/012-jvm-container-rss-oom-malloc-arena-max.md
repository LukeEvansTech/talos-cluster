# KB-012: JVM / Logstash Container RSS OOM Despite a Bounded Heap (`MALLOC_ARENA_MAX`)

**Status:** Resolved. The fix (`MALLOC_ARENA_MAX=2`) is a reusable first move for **any**
Java/JRuby/Logstash workload that OOMs on these many-core nodes.

## Symptom

A JVM-based pod (here `sentinel-syslog`, a Logstash → Microsoft Sentinel shipper) is
**OOMKilled (`exit 137`) on a regular cadence** (e.g. ~every 4h), creeping steadily toward the
container memory limit, even though the heap is explicitly bounded (`-Xmx512m`) and never near
its cap. Restarting just resets the clock.

## Cause

The growth is **off-heap / native**, and it scales with the node's core count. Diagnose by
attributing where RSS actually lives **before** changing anything:

- `kubectl exec ... -- curl -s localhost:9600/_node/stats/jvm`: if **heap and non-heap are
  both flat** while RSS climbs, the leak is native, not JVM.
- `/proc/1/smaps` (sum `Rss` per mapping): the tell here was **six ~64 MB anonymous arenas**
  (~380 MB and growing) on top of the JVM heap.

That pattern is **glibc per-thread malloc arenas**. On a 24-core node glibc spawns up to
`8 × ncpu` per-thread 64 MB arenas for native allocations (JRuby, the Azure SDK's constant
token exchange, Netty native) and **never returns them to the OS**. This is bounded by neither
`-Xmx` nor `-XX:MaxDirectMemorySize`, and `MALLOC_ARENA_MAX` was unset.

Two **secondary** contributors also inflate native memory on these nodes (worth fixing, but
not the dominant cause): Logstash defaults `pipeline.workers` to `availableProcessors()` (24
worker threads + Netty arenas sized for 24 cores), and the syslog input's Netty direct buffers
are uncapped.

## Fix

Cap the glibc arenas with an env var on the container:

```yaml
env:
  MALLOC_ARENA_MAX: "2"
```

Result here: arenas 6 → 3, malloc footprint ~380 MB → ~153 MB, the ~138 Mi/h creep went to 0,
and RSS **plateaued at ~629 Mi** (well under the 1536 Mi limit). If a workload still creeps
after that, escalate to jemalloc via `LD_PRELOAD`.

Address the secondary contributors too when relevant: pin `pipeline.workers: 2` in
`logstash.yml` (a syslog → Sentinel shipper needs far less than 24), and bound Netty with
`-XX:MaxDirectMemorySize` in `LS_JAVA_OPTS`.

## Lessons (generic)

- **`MALLOC_ARENA_MAX=2` is often THE fix** for JVM-in-container RSS bloat on many-core,
  no-CPU-limit nodes. Reach for it first.
- **Attribute where RSS lives *before* fixing.** `/proc/1/smaps` (look for many ~64 MB anon
  arenas) and the JVM stats endpoint (heap vs non-heap flat while RSS climbs = native malloc).
  The first attempt here fixed secondary layers because it skipped this step.
- **Verify a memory fix by watching the slope flatten** over 30-60 min, not by the low reading
  right after deploy. Distinguish JVM warmup (heap filling toward `-Xmx`, bounded) from a real
  native creep.

## References

- glibc `MALLOC_ARENA_MAX`: <https://www.gnu.org/software/libc/manual/html_node/Memory-Allocation-Tunables.html>
- Logstash tuning: <https://www.elastic.co/guide/en/logstash/current/tuning-logstash.html>
