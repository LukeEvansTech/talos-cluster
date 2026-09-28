# KB-085: lurcher ran out of memory every day for 12 days, invisible to its own health check

**Status:** Resolved (#5465, #5479).

## Symptom

lurcher scrapes contract listings once a day and sends alerts by Pushover. From 2026-09-16
it sent nothing for twelve days, and nothing paged. The pod was not crash-looping by
Kubernetes' definition: it crashed once a day, restarted, and `/health` answered OK again
in between.

## Cause

Node sizes its default heap to about half the container's memory limit, so the 512Mi limit
in place gave a heap of about 256 MB. A new source added in lurcher 2.2.0, Quality
Contracts, added about 24,000 listings against roughly 1,000 from the existing source, and
kept growing by about 450 a day. Loading a source's listings into memory in one pass hit
that heap ceiling and crashed the process with `FATAL ERROR: Reached heap limit ... JavaScript
heap out of memory`, once per day, straight after the Quality Contracts dump. Kubernetes'
`KubePodCrashLooping` needs faster, repeated restarts than a once-a-day crash produces, and
the liveness probe only checks that the process answers, which it does again as soon as it
restarts. Both stayed quiet through all twelve days.

## Fix

The memory limit was raised from 512Mi to 1Gi, and `NODE_OPTIONS=--max-old-space-size=768`
pins an explicit heap ceiling: about three times the point where it was dying, and below
the container limit so native memory (SQLite, buffers) still has room. That is a stopgap;
the underlying fix is to stop loading a whole source into memory at once, which lurcher
2.3.0 does by walking listings in keyset-paged batches.

Catching the next failure no longer depends on the heap holding.
`lurcher_last_success_timestamp_seconds` records the time of the last run that actually
delivered (closed success, at least one board scraped, no send failures), and
`LurcherNotDelivering` pages when that age passes 36 hours, sized against a run that takes
about an hour and one tolerated missed day. `LurcherMetricsMissing` covers the metric
disappearing entirely, which the outcome alert cannot see.

## How to recognise fast

A liveness or readiness probe proves the process answers, not that its actual job
completed. A process that crashes slower than `KubePodCrashLooping`'s window, or that comes
back healthy between failures, looks fine from outside while doing no real work. For a
scheduled or batch-style job, alert on the age of its last successful completion as well
as process health, and pair that with an absence check so a fully dead exporter still
pages.

## References

- Heap ceiling raised: #5465.
- Delivery alerting added: #5479.
