# TrueNAS Monitoring Setup

This directory contains Prometheus ScrapeConfigs for monitoring external systems like TrueNAS using exporters.

## Overview

The monitoring setup uses TrueNAS SCALE's Docker integration to run Prometheus exporters, exposing metrics that Prometheus in the Kubernetes cluster can scrape.

## Exporters

Four exporters are configured:

1. **node-exporter** (port 9100) - System metrics (CPU, memory, network, filesystem)
2. **smartctl-exporter** (port 9633) - SMART disk health metrics
3. **truenas-graphite-exporter** (ingest 9109 / metrics 9108) - TrueNAS/ZFS-native metrics (ZFS ARC, per-dataset and per-disk I/O, temperatures), bridged from the built-in netdata Graphite output via [`Supporterino/truenas-graphite-to-prometheus`](https://github.com/Supporterino/truenas-graphite-to-prometheus)
4. **docker-state-exporter** (port 9419) - Per-container state, health, restart count, and OOM-killed status; drives the `truenas-docker.rules` alert group

The first two treat TrueNAS as a generic Linux host. The third surfaces the ZFS internals that node-exporter cannot see: ARC size, hit ratio, and per-disk I/O, which are the highest-value storage signals. The fourth enables container lifecycle alerting by name (the graphite bridge's `cgroup_*` series are keyed by container ID and disappear once a container stops).

## TrueNAS Setup

### 1. Install Node Exporter

1. Navigate to **Apps** in TrueNAS SCALE
2. Click **Discover Apps**
3. Search for "Custom App"
4. Click **Install**
5. In the installation form, select **Install via YAML**
6. Paste the following configuration:

```yaml
version: "3"

services:
    node-exporter:
        image: quay.io/prometheus/node-exporter:latest
        container_name: node-exporter
        restart: unless-stopped
        network_mode: host
        pid: host
        command:
            - "--path.rootfs=/host/root"
            - "--path.procfs=/host/proc"
            - "--path.sysfs=/host/sys"
        volumes:
            - /:/host/root:ro
            - /proc:/host/proc:ro
            - /sys:/host/sys:ro
```

Then click **Install**.

### 2. Install SMARTCTL Exporter

1. Repeat the process for a new Custom App
2. Select **Install via YAML**
3. Paste the following configuration:

```yaml
version: "3"

services:
    smartctl-exporter:
        image: quay.io/prometheuscommunity/smartctl-exporter:latest
        container_name: smartctl-exporter
        restart: unless-stopped
        network_mode: host
        privileged: true
        user: root
        ports:
            - "9633:9633"
```

Then click **Install**.

### 3. Install TrueNAS Graphite Exporter (ZFS / netdata metrics)

This exporter bundles `prometheus/graphite_exporter` with a TrueNAS-specific mapping. TrueNAS's built-in netdata pushes Graphite-formatted metrics into it (ingest port `9109`); it re-exposes them as Prometheus metrics on port `9108`.

1. Repeat the Custom App process. Set the image to `ghcr.io/supporterino/truenas-graphite-to-prometheus:latest`.
2. Add two port mappings (host network or node ports):
    - Container `9109` → host `9109` (TCP): Graphite ingest
    - Container `9108` → host `9108` (TCP): Prometheus metrics
3. Click **Install**.

Then point TrueNAS at it:

1. Deploy the project's `netdata.conf` to `/etc/netdata/netdata.conf` on TrueNAS (enables the netdata Graphite backend; see the [upstream `TRUENAS.md`](https://github.com/Supporterino/truenas-graphite-to-prometheus/blob/main/TRUENAS.md)).
2. In TrueNAS go to **Reporting → Exporters → Add** and create a **Graphite** exporter:
    - **Destination IP / Port**: the exporter host and `9109`
    - **Prefix**: `truenas`
    - **Update every**: match your Prometheus scrape interval

> The exporter ships with the mapping config baked into the image, so no mapping file mount is required.

**Preferred (no hand-edited config):** create the Reporting exporter via the REST API instead of the
GUI. It reconfigures the built-in netdata's exporting engine the supported way, with no
`/etc/netdata/netdata.conf` hand-edit (which TrueNAS middleware manages/overwrites):

```bash
midclt call reporting.exporters.create '{"enabled": true, "name": "prometheus-graphite",
  "attributes": {"exporter_type": "GRAPHITE", "destination_ip": "127.0.0.1",
  "destination_port": 9109, "prefix": "truenas", "namespace": "truenas",
  "update_every": 10, "send_names_instead_of_ids": true, "matching_charts": "*"}}'
```

> **Do NOT** deploy the upstream `netdata.conf` on 25.10 unless you accept the trade-off: it
> switches netdata to vanilla charts (so the upstream dashboards match) but degrades the native
> TrueNAS **Reporting UI** and is overwritten on update. `prefix` MUST be `truenas` (the bridge
> mapping hardcodes it).

### 4. Verify Exporters are Running

From the TrueNAS shell or via SSH:

```bash
# Test node-exporter
curl http://localhost:9100/metrics

# Test smartctl-exporter
curl http://localhost:9633/metrics

# Test truenas-graphite-exporter (should show truenas_arcstats, disk_io, cpu_temperature,
# cgroup_*, nfs_*, interface_* series once netdata is pushing — see "Metric reality" below)
curl http://localhost:9108/metrics
```

### 5. Configure TrueNAS Firewall (if needed)

If TrueNAS has a firewall enabled, ensure ports 9100, 9633, and 9108 are accessible from your Kubernetes cluster network. Port 9109 only needs to be reachable from netdata on the TrueNAS host itself.

## Metric reality on TrueNAS SCALE 25.10 (verified 2026-06-14)

25.10's netdata emits a **reduced, custom-named** chart set, so the bridge does **not** reproduce
the full vanilla-netdata metric set the upstream mapping + dashboards assume. Confirmed against live
`/metrics` on `<storage-host>`:

| Area                     | Present                                             | Notes                                                                                                                                                           |
| ------------------------ | --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Disk I/O                 | `disk_io`, `disk_io_ops`, `disk_busy`               | no `disk_await/utilization/qops/size/iotime/backlog`                                                                                                            |
| CPU temp                 | `cpu_temperature`                                   | ✅                                                                                                                                                              |
| cgroups / k8s            | `cgroup_*`                                          | ✅ full                                                                                                                                                         |
| NFS / network / services | `nfs_*`, `interface_*`, `services_*`, `system_load` | ✅                                                                                                                                                              |
| ZFS ARC                  | `truenas_arcstats{type=...}`                        | raw chart, **not** vanilla `zfs.arc_size`; bridged to `zfs_arc_size` / `zfs_arc_free_bytes` / `zfs_arc_hit_ratio` via recording rules in `prometheusrule.yaml` |
| ZFS pool state           | **absent**                                          | no `zfs_pool`/scrub metric; rely on TrueNAS native ZFS event detection + alerts                                                                                |
| Disk temperature         | **absent** from graphite                            | use `smartctl_device_temperature` (smartctl-exporter); the disk-temp alert is repointed there                                                                  |
| Memory / detailed CPU    | **absent**                                          | `physical_memory`/`memory_*`/`cpu_frequency` not emitted                                                                                                        |

Consequence for the bundled dashboards: **cgroups** is fully populated; **disk_insights**,
**temperatures**, and the main **truenas_scale** dashboard are partial (empty panels for the absent
metrics); **applications_k3s** was removed (it targets `k3s_pod_*`; this box runs Talos separately).
A faithful ZFS dashboard needs either the netdata.conf swap (declined above) or a bespoke dashboard.

## Kubernetes Configuration

ScrapeConfigs are automatically discovered by kube-prometheus-stack. They are split across two locations:

- `kube-prometheus-stack/app/scrapeconfig.yaml`: the host-level exporters
- `truenas-exporter/app/scrapeconfig.yaml`: the Graphite exporter (this app also carries its dashboards and alert rules)

### ScrapeConfig Resources

- **node-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9100`
- **smartctl-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9633`
- **truenas-graphite-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9108`
- **truenas-docker-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9419`

These use the `SECRET_STORAGE_SERVER` variable from cluster secrets to dynamically configure the target hostname.

### Dashboards and Alerts (GitOps-managed)

Dashboards and alert rules are reconciled by Flux. No manual import. The `truenas-exporter` app provisions:

- **GrafanaDashboard** CRs (`grafanadashboard.yaml`), two dashboards:
  - `truenas-zfs` (bespoke): sourced from local ConfigMap `truenas-zfs-dashboard` / `truenas-zfs.json`, datasource input `DS_PROMETHEUS`. Primary ZFS/storage dashboard consuming the metrics this box actually emits.
  - `truenas-scale-cgroups`: fetched from upstream v2.2.1 (`truenas_scale_cgroups.json`), with datasource input `DS_MIMIR` remapped onto the cluster `prometheus` datasource. The dashboards `truenas_scale`, `disk_insights`, `temperatures`, and `applications_k3s` are retired. They render partially or fully blank on TrueNAS SCALE 25.10.
- A **PrometheusRule** (`prometheusrule.yaml`) with two alert groups:
  - `truenas-exporter.rules`: exporter-down (`TrueNASGraphiteExporterDown`), disk over-temperature (`TrueNASDiskTemperatureHigh`, sourced from smartctl-exporter), and CPU over-temperature (`TrueNASCPUTemperatureHigh`). No ZFS-pool-unhealthy alert: TrueNAS 25.10 exposes no `zfs_pool`/scrub-state metric via the graphite bridge; pool degradation is handled by TrueNAS's own native ZFS event detection.
  - `truenas-docker.rules`: six container lifecycle alerts fed by docker-state-exporter (`TrueNASDockerStateExporterDown`, `TrueNASContainerDown`, `TrueNASContainerUnhealthy`, `TrueNASContainerRestarting`, `TrueNASContainerOOMKilled`, `TrueNASContainerFlapping`).

## Grafana Dashboards

All dashboards are provisioned as `GrafanaDashboard` CRs via grafana-operator. Do not import by hand, or changes will be overwritten on reconcile.

| Dashboard                                                    | Source                   | Provisioned by                                                  |
| ------------------------------------------------------------ | ------------------------ | --------------------------------------------------------------- |
| Node Exporter Full                                           | grafana.com 1860         | `kube-prometheus-stack/app/grafanadashboard-node-exporter.yaml` |
| SMART Disk Monitoring                                        | grafana.com 22604        | `smartctl-exporter/app/grafanadashboard.yaml`                   |
| TrueNAS ZFS (bespoke)                                        | local ConfigMap          | `truenas-exporter/app/grafanadashboard.yaml`                    |
| TrueNAS SCALE Cgroups                                        | upstream v2.2.1 JSON     | `truenas-exporter/app/grafanadashboard.yaml`                    |

To add another, drop a new `GrafanaDashboard` CR into the relevant app and let Flux reconcile.

## Metrics Examples

### Node Exporter Metrics

```promql
# CPU usage
100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# Memory usage
100 - ((node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100)

# Disk usage
100 - ((node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100)

# Network traffic
rate(node_network_receive_bytes_total{device="eth0"}[5m])
rate(node_network_transmit_bytes_total{device="eth0"}[5m])
```

### SMART Exporter Metrics

```promql
# Disk temperature
smartctl_device_temperature

# Disk health status
smartctl_device_smart_healthy

# Power on hours
smartctl_device_power_on_seconds / 3600

# Reallocated sectors
smartctl_device_attribute_value{attribute_name="Reallocated_Sector_Ct"}
```

### TrueNAS Graphite Exporter Metrics (ZFS)

These come from the netdata Graphite bridge (`job="truenas-graphite-exporter"`). See the upstream [`METRICS.md`](https://github.com/Supporterino/truenas-graphite-to-prometheus/blob/main/METRICS.md) for the full list; exact label keys depend on your netdata version, so confirm against live `/metrics`.

```promql
# ZFS ARC size
zfs_arc_size{job="truenas-graphite-exporter"}

# ZFS ARC hit rate
zfs_hits_rate{job="truenas-graphite-exporter"}

# ZFS pool state (dimension/value encoding is netdata-specific — verify)
zfs_pool{job="truenas-graphite-exporter"}

# Per-disk temperature
disk_temperature{job="truenas-graphite-exporter"}

# CPU temperature
cpu_temperature{job="truenas-graphite-exporter"}
```

## Troubleshooting

### Exporters Not Starting

1. Check the app status in TrueNAS **Apps** page
2. View logs by clicking on the app and selecting **Logs**
3. Restart the app if needed using the **Stop/Start** buttons

### Prometheus Not Scraping

```bash
# Check ScrapeConfig is applied
kubectl get scrapeconfig -n observability

# Check Prometheus targets
# Go to Prometheus UI → Status → Targets
# Look for node-exporter, smartctl-exporter, and truenas-graphite-exporter jobs
```

### Metrics Not Showing in Grafana

1. Verify Prometheus is scraping targets successfully
2. Check that the job labels match in your queries
3. Ensure the time range in Grafana includes recent data

## Maintenance

### Updating Exporters

1. Navigate to the **Apps** page in TrueNAS
2. Click on the app you want to update
3. Click **Update** if a new version is available

### Removing Exporters

1. Navigate to the **Apps** page in TrueNAS
2. Click on the app you want to remove
3. Click **Delete**

## References

- [Node Exporter Documentation](https://github.com/prometheus/node_exporter)
- [SMARTCTL Exporter Documentation](https://github.com/prometheus-community/smartctl_exporter)
- [Prometheus Documentation](https://prometheus.io/docs/introduction/overview/)
