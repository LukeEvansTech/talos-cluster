# Kopia

## Purpose

`kopia` in `volsync-system` is the shared repository server every app's VolSync
`ReplicationSource` backs up into. The chart mounts an NFS-backed volume as `/repository`; the
export's address comes from Flux `postBuild` substitution, not from a literal value in this
repository.

## Index blob exporter

The `index-exporter` container is a sidecar on the kopia pod rather than a separate app,
because it needs the same repository mount kopia already has. It never talks to the kopia
server process; it only walks the repository filesystem and counts files.

It exists because kopia warns `Found too many index blobs` and degrades every repository
operation once these accumulate, but exposes no metrics endpoint of its own. Before this
exporter, the count only showed up in the maintenance job's log output, so nothing could alert
on it ([#4009](https://github.com/LukeEvansTech/talos-cluster/pull/4009)).

The repository's index blobs come in three kinds, confirmed against the live repository:

- `xn*`: uncompacted epoch indices, the ones that pile up
- `xs*`: single-epoch compacted
- `xr*`: range-compacted

The exporter publishes `kopia_repo_index_blobs{kind="..."}` split by kind rather than as one
summed total, because a rising total only matters when it is the uncompacted set doing the
rising. That is the signal that compaction is losing to snapshot creation.

Counting happens on a slow loop and is cached, not done per scrape, because it is a directory
walk over an NFS export shared with every backup in the cluster. The `ServiceMonitor`'s scrape
reads that cached, already-computed file, so it never touches the NFS export itself.

The exporter script doubles every shell variable reference (`$$var`), because Flux's
`postBuild` substitution runs over this manifest before it reaches the cluster: a single `$`
either gets blanked by `envsubst` or fails the build outright, and a blanked variable would
render an empty `kind` label with a count that never changes. Check the rendered output, not
just the source, after editing this script.

## Alert design

`KopiaIndexBlobsHigh` is a single alert on an absolute ceiling
(`sum(kopia_repo_index_blobs) > 10000`), because every severity on this rule pages, so it has
to fire approximately never.

About 100 `ReplicationSource` objects share this one repository, and a single maintenance run
only compacts a bounded number of epochs, so creation can outpace compaction. That happened on
2026-07-30, when the uncompacted count grew from 2,803 to 4,048 over 16 hours on a 4-hour
maintenance cadence; the fix was moving maintenance to hourly
([KB-016](../troubleshooting/kb/016-kopia-repo-server-oom-repo-size.md) covers the related
repository-size incident). 10,000 sits at roughly 2.5x the peak that incident reached, so the
alert only fires if hourly maintenance is clearly losing, not merely lagging behind a burst.

The rule is deliberately not a growth-rate alert. Per-run deltas are lumpy (+6 one run, +618
the next), so a rate threshold would page on noise instead of on the actual failure mode.
`for: 2h` requires at least two maintenance runs to fail to bring the count back down, so one
slow run does not page.

If this fires, the next lever is backup retention (`hourly: 168` in `components/volsync/nfs`),
not maintenance frequency: maintenance is already hourly.

## Scrape and metrics quirks

The exporter serves its metrics as a static `.txt` file over `busybox httpd`, and the
extension is load-bearing. `busybox httpd` sets `Content-Type` from a built-in table that
knows `.txt` (`text/plain`) but not `.prom`, and Prometheus 3 marks a target down for
"sending blank Content-Type" unless a `fallback_scrape_protocol` is set, which this operator's
`ServiceMonitor` CRD does not expose. Keep the extension if this file, or the
`ServiceMonitor`'s `path`, is ever changed.

## Storage

The `repository` volume uses `advancedMounts` rather than `globalMounts` so the exporter's
mount can be `readOnly: true` while kopia's own mount stays read-write. The exporter only ever
counts files, and nothing that merely observes the backup repository should be able to write
to it.

## Related

- [KB-016](../troubleshooting/kb/016-kopia-repo-server-oom-repo-size.md), the server OOMs on
  repository size, not on a maintenance failure.
- [KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md), sizing the shared
  cache PVC that every app's mover uses against this same repository.
- [Known noise](../troubleshooting/known-noise.md): never run `kopia` CLI commands inside the
  server pod. `snapshot list --all` loads the full index into the serving container and
  OOMKills it.
