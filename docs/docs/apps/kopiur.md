# Kopiur

[Kopiur](https://github.com/home-operations/kopiur) is a Kopia-native Kubernetes backup operator.
It is a trial in this cluster, running alongside VolSync rather than replacing it, and is scoped to
two small apps while it proves out.

## Purpose

- Local NFS CSI snapshotter, running alongside VolSync. VolSync is untouched and keeps owning the
  app PVC plus its own offsite copy; kopiur has no offsite backend of its own, so a full VolSync
  removal is not an option here.
- Trial scope is `apprise` and `atuin` in the `default` namespace, plus the operator's own
  `kopiur-system` namespace.
- Alpha operator, with fragility accepted for the trial: the webhook's `failurePolicy: Fail` means
  the `apprise`/`atuin` Kustomization applies depend on operator health, so they may fail and retry
  until the operator is up.

## Design decisions

- **ESO credential model.** `credentialProjection` stays at its chart default (disabled); movers
  read `KOPIA_PASSWORD` from a per-namespace ExternalSecret (`components/kopiur/secret`) instead of
  the operator copying credentials into app namespaces. The chart's own RBAC (cluster-wide secrets
  create/update/patch, needed for its self-managed webhook TLS rotation) is unchanged either way, so
  this is a behavioural choice rather than an RBAC restriction. That RBAC claim is carried forward
  from the original design decision and was not re-verified against the chart's rendered RBAC in
  this pass.
- **No controller repository mount.** Maintenance and verification run in mover pods, which get the
  backend from the `ClusterRepository`. Coupling controller liveness to NFS would be a
  self-inflicted outage mode, and it also matches how upstream home-ops runs the same chart version.
- **NFS backend layout.** `spec.backend.filesystem.path` is only the mountpoint inside mover pods;
  isolating this repository from the separate volsync kopia repository on the same NFS export
  happens by mounting a dedicated subdirectory, pre-created on the NAS, not by this field. Promote
  it to its own TrueNAS dataset if the trial is adopted permanently.
- **Trial-scoped `allowedNamespaces`.** `default` (`apprise`, `atuin`) plus `kopiur-system`, since
  the webhook gates the operator's own `Maintenance` CR against this same list. Widen it once the
  trial proves out.
- **Unchanged `identityDefaults`.** Kept the same since the 0.4.x trial so existing snapshots stay
  addressable under the same kopia identity across chart bumps.
- **Centralised timezone.** `scheduleDefaults.timezone` sets the timezone once (DRY) instead of
  repeating it per `SnapshotSchedule`.
- **Unset mover cache/scratch.** `moverDefaults` leaves `cache`/`scratch` unset on purpose, so both
  fall back to an `emptyDir`, which fits these tiny trial apps. Size real PVCs only once bigger apps
  join (a `cache` CRD example uses 10Gi; deep-verify scratch must hold a full restore). A token value
  like 1Mi would fill up (ENOSPC) immediately.
- **Maintenance schedule tuning ([#5014](https://github.com/LukeEvansTech/talos-cluster/pull/5014)).**
  Quick maintenance runs hourly (`0 * * * *`, 10m jitter) instead of the operator default (every 6
  hours, 30m jitter), to keep index blob growth bounded as the trial widens. `full` restates the
  operator default (daily at 03:00, 1h jitter) because the CRD requires both halves of `schedule`
  once either is set. No `timezone` is set inside `maintenance.schedule` on purpose: maintenance
  crons fall back to the controller's UTC default, and `scheduleDefaults.timezone` above governs
  `SnapshotSchedule`s only, so naming a timezone there would silently shift `full` by an hour.
- **1h epoch floor.** `parameters.epoch.minDuration: 1h` (kopia's own default is 24h) pairs with the
  hourly quick maintenance above, so an epoch closes and compacts the same day instead of
  accumulating a day of index blobs first.

## Upgrade notes

- Chart 0.6.0 nested `metrics.serviceMonitor`, `metrics.prometheusRule`, and `grafanaDashboard.*`
  under a single `monitoring:` umbrella. That rename is the only breaking values change between the
  trial's original 0.4.8 install and its resume on 0.7.0.
- 0.7.0's other breaking changes (Rust dependency churn, replication-mover credential handling)
  don't apply here, since this deployment uses neither.
