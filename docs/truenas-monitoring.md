# TrueNAS Monitoring Setup

This directory contains Prometheus ScrapeConfigs for monitoring external systems like TrueNAS using exporters.

## Overview

The monitoring setup uses TrueNAS SCALE's Docker integration to run Prometheus exporters, exposing metrics that Prometheus in the Kubernetes cluster can scrape.

## Exporters

Two exporters are configured:

1. **node-exporter** (port 9100) - System metrics (CPU, memory, network, filesystem)
2. **smartctl-exporter** (port 9633) - SMART disk health metrics

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

### 3. Verify Exporters are Running

From the TrueNAS shell or via SSH:

```bash
# Test node-exporter
curl http://localhost:9100/metrics

# Test smartctl-exporter
curl http://localhost:9633/metrics
```

### 4. Configure TrueNAS Firewall (if needed)

If TrueNAS has a firewall enabled, ensure ports 9100 and 9633 are accessible from your Kubernetes cluster network.

## Kubernetes Configuration

The Prometheus ScrapeConfigs are defined in `app/scrapeconfig.yaml` and automatically discovered by kube-prometheus-stack.

### ScrapeConfig Resources

- **node-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9100`
- **smartctl-exporter**: Scrapes `${SECRET_STORAGE_SERVER}:9633`

These use the `SECRET_STORAGE_SERVER` variable from cluster secrets to dynamically configure the target hostname.

## Grafana Dashboards

### Recommended Dashboards

Import these community dashboards in Grafana:

1. **Node Exporter Full** - Dashboard ID: 1860
   - Comprehensive system metrics (CPU, memory, disk, network)

2. **Node Exporter for Prometheus** - Dashboard ID: 11074
   - Simplified system overview

3. **SMART Disk Monitoring** - Dashboard ID: 10530
   - Disk health and SMART attributes

### Import via Grafana UI

1. Go to Grafana → Dashboards → Import
2. Enter the Dashboard ID
3. Select the Prometheus data source
4. Click Import

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
# Look for node-exporter and smartctl-exporter jobs
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
