# TrueNAS Monitoring Setup

This directory contains Prometheus ScrapeConfigs for monitoring external systems like TrueNAS using exporters.

## Overview

The monitoring setup uses TrueNAS SCALE's Docker integration to run Prometheus exporters, exposing metrics that Prometheus in the Kubernetes cluster can scrape.

## Exporters

Three exporters are configured:

1. **node-exporter** (port 9100) - System metrics (CPU, memory, network, filesystem)
2. **smartctl-exporter** (port 9633) - SMART disk health metrics
3. **truenas-graphite-exporter** (ingest 9109 / metrics 9108) - TrueNAS/ZFS-native metrics (ZFS pool state, ARC, per-dataset and per-disk I/O, temperatures), bridged from the built-in netdata Graphite output via [`Supporterino/truenas-graphite-to-prometheus`](https://github.com/Supporterino/truenas-graphite-to-prometheus)

The first two treat TrueNAS as a generic Linux host. The third surfaces the ZFS internals that node-exporter cannot see — pool health, ARC hit ratio, scrub state — which are the highest-value storage signals.

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

7. Click **Install**

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

4. Click **Install**

### 3. Install TrueNAS Graphite Exporter (ZFS / netdata metrics)

This exporter bundles `prometheus/graphite_exporter` with a TrueNAS-specific mapping. TrueNAS's built-in netdata pushes Graphite-formatted metrics into it (ingest port `9109`); it re-exposes them as Prometheus metrics on port `9108`.

1. Repeat the Custom App process. Set the image to `ghcr.io/supporterino/truenas-graphite-to-prometheus:latest`.
2. Add two port mappings (host network or node ports):
   - Container `9109` → host `9109` (TCP) — Graphite ingest
   - Container `9108` → host `9108` (TCP) — Prometheus metrics
3. Click **Install**.

Then point TrueNAS at it:

1. Deploy the project's `netdata.conf` to `/etc/netdata/netdata.conf` on TrueNAS (enables the netdata Graphite backend; see the [upstream `TRUENAS.md`](https://github.com/Supporterino/truenas-graphite-to-prometheus/blob/main/TRUENAS.md)).
2. In TrueNAS go to **Reporting → Exporters → Add** and create a **Graphite** exporter:
   - **Destination IP / Port**: the exporter host and `9109`
   - **Prefix**: `truenas`
   - **Update every**: match your Prometheus scrape interval

> The exporter ships with the mapping config baked into the image, so no mapping file mount is required. Only `netdata.conf` and the Reporting exporter need to be configured TrueNAS-side.

### 4. Verify Exporters are Running

From the TrueNAS shell or via SSH:

```bash
# Test node-exporter
curl http://localhost:9100/metrics

# Test smartctl-exporter
curl http://localhost:9633/metrics

# Test truenas-graphite-exporter (should show zfs_*, disk_*, cpu_* series once netdata is pushing)
curl http://localhost:9108/metrics
```

### 5. Configure TrueNAS Firewall (if needed)

If TrueNAS has a firewall enabled, ensure ports 9100, 9633, and 9108 are accessible from your Kubernetes cluster network. Port 9109 only needs to be reachable from netdata on the TrueNAS host itself.

## Kubernetes Configuration

ScrapeConfigs are automatically discovered by kube-prometheus-stack. They are split across two locations:

- `kube-prometheus-stack/app/scrapeconfig.yaml` — the host-level exporters
- `truenas-exporter/app/scrapeconfig.yaml` — the Graphite exporter (this app also carries its dashboards and alert rules)

### ScrapeConfig Resources

- **node-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9100`
- **smartctl-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9633`
- **truenas-graphite-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9108`

These use the `SECRET_STORAGE_SERVER` variable from cluster secrets to dynamically configure the target hostname.

### Dashboards and Alerts (GitOps-managed)

Dashboards and alert rules are reconciled by Flux — no manual import. The `truenas-exporter` app provisions:

- **GrafanaDashboard** CRs (`grafanadashboard.yaml`) pulling the five upstream dashboards (`truenas_scale`, `disk_insights`, `temperatures`, `cgroups`, `applications_k3s`) pinned to a release tag, with the dashboards' `DS_MIMIR` datasource input remapped onto the cluster `prometheus` datasource.
- A **PrometheusRule** (`prometheusrule.yaml`) with exporter-down, ZFS-pool-unhealthy, and disk/CPU over-temperature alerts.

> The ZFS-pool alert's `zfs_pool` label/value encoding (netdata dimensions) should be confirmed against live `/metrics` output — see the inline note in `prometheusrule.yaml`.

## Grafana Dashboards

All dashboards are provisioned as `GrafanaDashboard` CRs via grafana-operator — do not import by hand, or changes will be overwritten on reconcile.

| Dashboard | Source | Provisioned by |
| --- | --- | --- |
| Node Exporter Full | grafana.com 1860 | `kube-prometheus-stack/app/grafanadashboard-node-exporter.yaml` |
| SMART Disk Monitoring | grafana.com 22604 | `smartctl-exporter/app/grafanadashboard.yaml` |
| TrueNAS SCALE (+ disk insights, temperatures, cgroups, apps) | upstream repository JSON | `truenas-exporter/app/grafanadashboard.yaml` |

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
