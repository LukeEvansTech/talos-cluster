# Certwarden Certificate Deployment

Automated certificate deployment to network devices via Certwarden post-processing.

## Supported Devices

### ✅ IPMI (Supermicro X12/X13/H13)
- **Status**: Production Ready
- **API**: Redfish v1
- **Models**: X12, X13, H13 only

### 🚧 APC UPS
- **Status**: Coming Soon

## How It Works

```mermaid
sequenceDiagram
    participant CW as Certwarden
    participant Job as Kubernetes Job
    participant Device as Target Device

    CW->>CW: Certificate Renewed
    CW->>Job: Execute Post-Process Script
    Job->>Job: Create Deployment Job
    Job->>Device: Upload Certificate
    Device-->>Job: Success
    Job->>Job: Auto-cleanup (5min TTL)
```

## Directory Structure

```
cert-deployment/
├── README.md              # This file
├── kustomization.yaml     # Includes device types
├── ipmi/                  # IPMI deployment
│   ├── kustomization.yaml
│   ├── externalsecret.yaml
│   ├── rbac.yaml
│   ├── ipmi-updater.py
│   └── certwarden-ipmi-deploy.sh
└── apc/                   # APC deployment (future)
    └── README.md
```

## Quick Start

### IPMI Setup

1. **Add IPMI credentials to 1Password**:
   - Item name: `ipmi-{hostname}`
   - Fields: `IPMI_URL`, `IPMI_MODEL`, `IPMI_USERNAME`, `IPMI_PASSWORD`

2. **Update ExternalSecret** in `ipmi/externalsecret.yaml`:
   ```yaml
   dataFrom:
     - extract:
         key: ipmi-{hostname}  # Your 1Password item name
   ```

3. **Deploy**:
   ```bash
   kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/
   ```

4. **Configure Certwarden** (via UI):
   - Certificate → Post-Processing
   - Script: `/app/scripts/certwarden-ipmi-deploy.sh`
   - Environment: `IPMI_HOST={hostname}` (matches secret name)

5. **Test**: Force certificate renewal in Certwarden UI

## Monitoring

```bash
# Watch for jobs
kubectl get jobs -n infrastructure -w

# View logs
kubectl logs -n infrastructure -l app.kubernetes.io/name=certwarden-ipmi-deploy -f
```

## Security

- Credentials from 1Password via ExternalSecrets
- Non-root containers
- Minimal RBAC permissions
- Auto-cleanup after 5 minutes
- SSL verification disabled (required for self-signed IPMI certs)

## Troubleshooting

**Job fails?**
```bash
kubectl logs -n infrastructure job/<job-name>
```

**ExternalSecret not syncing?**
```bash
kubectl describe externalsecret -n infrastructure ipmi-{hostname}
```

**Force ExternalSecret sync:**
```bash
kubectl annotate externalsecret -n infrastructure ipmi-{hostname} \
  force-sync=$(date +%s) --overwrite
```

---

**Last Updated**: 2025-11-21
**Status**: IPMI Production Ready
