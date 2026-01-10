# AC Infinity Prometheus Exporter - Build Briefing

## Overview

Build a Prometheus exporter for AC Infinity UIS controllers (fans, grow tent equipment). The exporter polls the AC Infinity cloud API and exposes metrics for Prometheus scraping.

## API Reference

Based on reverse-engineering from [homeassistant-acinfinity](https://github.com/dalinicus/homeassistant-acinfinity).

### Authentication

```
POST http://www.acinfinityserver.com/api/user/appUserLogin
Content-Type: application/x-www-form-urlencoded

appEmail=<email>&appPasswordl=<password>
```

Note: The API has a typo - it's `appPasswordl` (with an 'l'), not `appPassword`.

**Response:**
```json
{
  "code": 200,
  "data": {
    "appId": "<user_token>",
    "appEmail": "...",
    "nickName": "..."
  }
}
```

### Get All Devices

```
POST http://www.acinfinityserver.com/api/user/devInfoListAll
Content-Type: application/x-www-form-urlencoded
token: <appId from login>

userId=<appId>
```

**Response structure:**
```json
{
  "code": 200,
  "data": [
    {
      "devId": "<controller_id>",
      "devName": "Controller 69",
      "devMacAddr": "...",
      "portResist": 4,
      "devTimeZone": "America/Chicago",
      "deviceInfo": {
        "temperature": 2345,      // divide by 100 for °C
        "humidity": 5500,         // divide by 100 for %
        "vpd": 123,               // divide by 100 for kPa
        "sensors": [...],         // external sensors
        "ports": [...]            // connected devices/fans
      }
    }
  ]
}
```

### Device Port Structure (fans, lights, etc.)

```json
{
  "port": 1,
  "portName": "Exhaust Fan",
  "speak": 7,           // current speed 0-10
  "online": 1,          // 0=offline, 1=online
  "state": 1,           // 0=off, 1=on
  "remainTime": 3600,   // seconds until mode change
  "devType": 11         // device type
}
```

### Sensor Structure (probes, CO2, etc.)

```json
{
  "accessPort": 1,
  "sensorType": 1,      // see sensor types below
  "sensorData": 750,    // raw value
  "sensorPrecis": 2,    // decimal places
  "sensorUnit": 1       // 0=F, 1=C for temp
}
```

### Sensor Types

| Type | Description |
|------|-------------|
| 1 | Probe Temperature (°F) |
| 2 | Probe Temperature (°C) |
| 3 | Probe Humidity |
| 4 | Probe VPD |
| 5 | Controller Temperature (°F) |
| 6 | Controller Temperature (°C) |
| 7 | Controller Humidity |
| 8 | Controller VPD |
| 9 | CO2 (ppm) |
| 10 | Light (%) |
| 12 | Soil Moisture (%) |

## Metrics to Expose

### Controller Metrics
```
acinfinity_controller_info{controller_id, controller_name, mac_address, timezone} 1
acinfinity_controller_temperature_celsius{controller_id, controller_name} 23.45
acinfinity_controller_humidity_percent{controller_id, controller_name} 55.00
acinfinity_controller_vpd_kpa{controller_id, controller_name} 1.23
```

### Device/Port Metrics
```
acinfinity_device_info{controller_id, port, device_name, device_type} 1
acinfinity_device_speed{controller_id, port, device_name} 7
acinfinity_device_online{controller_id, port, device_name} 1
acinfinity_device_state{controller_id, port, device_name} 1
acinfinity_device_remaining_seconds{controller_id, port, device_name} 3600
```

### Sensor Metrics
```
acinfinity_sensor_temperature_celsius{controller_id, port, sensor_type} 24.5
acinfinity_sensor_humidity_percent{controller_id, port, sensor_type} 60.0
acinfinity_sensor_vpd_kpa{controller_id, port, sensor_type} 1.15
acinfinity_sensor_co2_ppm{controller_id, port} 800
acinfinity_sensor_light_percent{controller_id, port} 75
acinfinity_sensor_soil_percent{controller_id, port} 45
```

### Exporter Metrics
```
acinfinity_api_requests_total{status} - counter
acinfinity_api_request_duration_seconds - histogram
acinfinity_last_scrape_timestamp - gauge
acinfinity_last_scrape_success - gauge (1=success, 0=failure)
```

## Implementation

### Dependencies

```
prometheus_client>=0.20.0
requests>=2.31.0
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ACINFINITY_EMAIL` | Yes | - | AC Infinity account email |
| `ACINFINITY_PASSWORD` | Yes | - | AC Infinity account password |
| `METRICS_PORT` | No | 8000 | Port to expose metrics |
| `POLL_INTERVAL` | No | 60 | Seconds between API polls |
| `LOG_LEVEL` | No | INFO | Logging level |

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

USER 65534:65534

EXPOSE 8000

ENTRYPOINT ["python", "-m", "src.main"]
```

### Project Structure

```
acinfinity-exporter/
├── .github/
│   └── workflows/
│       └── build.yaml          # Build and push to ghcr.io
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point, metrics server
│   ├── client.py               # AC Infinity API client
│   ├── metrics.py              # Prometheus metric definitions
│   └── collector.py            # Metrics collection logic
├── Dockerfile
├── requirements.txt
├── README.md
└── .gitignore
```

### Key Implementation Notes

1. **Token refresh**: The API token may expire. Re-authenticate on 401 responses.

2. **Temperature conversion**: API may return Fahrenheit. Convert to Celsius: `(F - 32) * 5/9`

3. **Value scaling**: Most values need division by 10^(precision-1) where precision comes from `sensorPrecis` field.

4. **Polling vs scrape**: Use a background thread to poll the API at `POLL_INTERVAL`. Prometheus scrapes just read cached values. This avoids hammering the API on every scrape.

5. **Rate limiting**: The AC Infinity API has rate limits. Default 60s poll interval is safe.

6. **Error handling**: API can be flaky. Implement retries with exponential backoff.

## GitHub Actions Workflow

```yaml
name: Build Container

on:
  push:
    branches: [main]
    paths:
      - 'acinfinity-exporter/**'
  pull_request:
    branches: [main]
    paths:
      - 'acinfinity-exporter/**'

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: ./acinfinity-exporter
          push: ${{ github.event_name != 'pull_request' }}
          tags: |
            ghcr.io/${{ github.repository_owner }}/acinfinity-exporter:latest
            ghcr.io/${{ github.repository_owner }}/acinfinity-exporter:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

## Kubernetes Deployment (for talos-cluster repo)

After the container is built, deploy in the talos-cluster repo:

```
kubernetes/apps/observability/acinfinity-exporter/
├── ks.yaml
└── app/
    ├── kustomization.yaml
    ├── helmrelease.yaml
    ├── externalsecret.yaml      # Credentials from secret store
    ├── servicemonitor.yaml
    ├── prometheusrule.yaml      # Alerts
    └── grafanadashboard.yaml
```

### Suggested Alerts

- `ACInfinityDeviceOffline` - Device port offline for >5 minutes
- `ACInfinityHighTemperature` - Temperature above threshold
- `ACInfinityLowHumidity` / `ACInfinityHighHumidity` - Humidity out of range
- `ACInfinityHighVPD` - VPD above optimal range
- `ACInfinityExporterDown` - Exporter not responding
- `ACInfinityAPIError` - API errors for >5 minutes

## Testing

1. Create `.env` file with credentials
2. Run locally: `python -m src.main`
3. Check metrics: `curl http://localhost:8000/metrics`
4. Build container: `docker build -t acinfinity-exporter .`
5. Run container: `docker run -p 8000:8000 --env-file .env acinfinity-exporter`
