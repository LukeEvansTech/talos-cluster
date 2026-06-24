# KB-016: Kopia Repo Server OOM = Repo Size, Not a Maintenance Failure

**Status:** Resolved (raised to 2Gi); revisit as the repo grows.

## Symptom

The `volsync-system/kopia` repository server **OOMKills in a crashloop** (`exit 137`) at its
memory limit — even a trivial `kopia maintenance info` exec OOMs. The server log shows
`Found too many index blobs ... run kopia maintenance`, which **looks like** maintenance is
broken.

## Cause

It's **repo size**, not maintenance. The VolSync Kopia repo had grown to ~320 GB / **3.4M
in-use contents** / ~3,462 index blobs. The server loads the **full index into memory on
start**, which exceeds the limit. The "too many index blobs" message is a **red herring** —
maintenance is actually running fine:

- The `KopiaMaintenance` CR drives the `kopia-maint-daily-*` cronjob every 4h.
- Its job log shows it owns maintenance, runs quick **then full** maintenance (GC + epoch
  compaction), and reports `MAINTENANCE_STATUS: SUCCESS` in a few minutes.
- The maintenance job pod has **no resource limits**, so it doesn't OOM — only the long-lived
  server did.

## Fix

Raise the server's memory in `kubernetes/apps/volsync-system/kopia/app/helmrelease.yaml`:

```yaml
resources:
  requests:
    memory: 1Gi      # floor
  limits:
    memory: 2Gi
```

The repo keeps growing, so if it OOMs again at 2Gi, go 3–4Gi. (Maintenance config lives at
`kubernetes/apps/volsync-system/volsync/maintenance/`.)

## References

- Kopia maintenance: <https://kopia.io/docs/advanced/maintenance/>
- Related OOM-by-load pattern: [KB-007](007-flux-not-ready-artifact-failed-alert-storms.md).
