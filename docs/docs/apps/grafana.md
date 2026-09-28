# Grafana

## Purpose

`kubernetes/apps/observability/grafana` runs grafana-operator plus a single Grafana instance
CR. Dashboards ship as declarative `GrafanaDashboard` custom resources across the repository,
and the Prometheus and Alertmanager datasources are wired in directly rather than through the
UI.

## Backups

The Grafana CR's `persistentVolumeClaim` block creates and owns the data volume itself
(`grafana-pvc`), rather than through the app-template persistence pattern most other apps use.

`instance/kustomization.yaml` references `components/volsync/nfs` and
`components/volsync/remote` directly instead of the parent `components/volsync` component.
The parent component also ships `pvc.yaml`, an app-template-style claim named `${APP}`
(`grafana`). Pulling that in here would create a second, unused PVC that would try to restore
from a `ReplicationDestination` with no snapshot yet, since nothing ever backs up to it.

Both `ReplicationSource` templates default `sourcePVC` to `${APP}`, which resolves to
`grafana`, not the operator's real claim. `instance/kustomization.yaml` patches that field
locally to `grafana-pvc` rather than changing the shared component.

What actually needs backing up is state that Git does not hold: the operator's SQLite
database, local users, preferences, annotations, and alert-rule state, plus any dashboard
built ad hoc in the UI instead of committed as a `GrafanaDashboard` CR. The dashboards already
tracked in this repository are declarative and do not depend on a PVC snapshot to recover.
