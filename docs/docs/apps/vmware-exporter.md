# VMware exporter

## Purpose

`kubernetes/apps/observability/vmware-exporter` wraps pryorda/vmware_exporter (v0.18.4) to scrape
one vCenter for host, datastore, VM and snapshot metrics. The ServiceMonitor supplies the target
vCenter as a `?vsphere_host=` query param rather than a container argument, so the exporter pod
does not need to know its target at startup. Three `GrafanaDashboard` CRs (cluster, ESXi, virtual
machine) and a `PrometheusRule` round out the app.

## Design

- Credentials come from a dedicated read-only vCenter account (the `vsphere-monitoring` 1Password
  item), not the `vsphere-terraform` admin account used for provisioning, a deliberate
  least-privilege split.
- `VSPHERE_IGNORE_SSL` is set because vCenter presents its own VMCA certificate, which clients do
  not trust by default.
- Pryorda's per-VM performance-counter pulls are slow, so the ServiceMonitor runs a long scrape
  interval and timeout (180s/120s, timeout below interval) and `VMwareExporterDown`'s 15m `for`
  rides out an occasional slow scrape rather than paging on it.
- The three dashboards are vendored from the upstream repository instead of imported via `url:`,
  because the upstream JSON hard-codes its `$datasource` template variable to `Prometheus`
  (capitalized), which does not match this cluster's `prometheus` datasource name.
  [KB-021](../troubleshooting/kb/021-grafana-dashboard-panels-blank-datasource-case.md) covers that
  bug in general. `kustomization.yaml`'s `configMapGenerator` also disables Flux's `postBuild`
  substitution on the generated ConfigMap, so it does not blank the dashboards' own
  `${datasource}`/`${__interval}` macros.

## Alert notes

- `VMwareHostPoweredOff` assumes `vmware_host_power_state == 1` means `poweredOn`. That has not
  been confirmed against live `/metrics` output, so treat it as provisional until checked.
- `VeeamServerGuestDown` watches one named VM's VMware Tools status rather than a generic
  host/datastore metric.
  [KB-069](../troubleshooting/kb/069-veeam-server-bsod-went-unnoticed-for-12-days.md) covers the
  outage that motivated it and why the alert has no `absent()` companion.
