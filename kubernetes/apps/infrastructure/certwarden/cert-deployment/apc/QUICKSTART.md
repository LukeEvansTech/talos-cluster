# APC NMC Certificate Deployment - Quick Start

Get your APC UPS certificate deployment up and running in 5 minutes.

## Prerequisites

- APC UPS with NMC2 or NMC3 network card
- SSH access to the APC NMC
- Admin credentials
- 1Password with External Secrets configured in your cluster

## Quick Setup

### 1. Get SSH Fingerprint

```bash
ssh-keyscan -H your-apc-hostname.local | ssh-keygen -lf -
```

Copy the fingerprint (the part after `SHA256:`).

### 2. Create 1Password Item

Create a new item named `apc-ups-main` with these fields:

| Field Name       | Example Value                          |
|------------------|----------------------------------------|
| APC_HOSTNAME     | ups.example.com                        |
| APC_USERNAME     | apc                                    |
| APC_PASSWORD     | your-password                          |
| APC_FINGERPRINT  | ABC123def456...                        |

### 3. Edit External Secret

Edit `externalsecret.yaml` if your 1Password item has a different name:

```yaml
metadata:
  name: apc-ups-main  # Change to match your 1Password item
spec:
  target:
    name: apc-ups-main  # Change to match your 1Password item
  dataFrom:
    - extract:
        key: apc-ups-main  # Change to match your 1Password item
```

### 4. Deploy

```bash
kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/apc/
```

### 5. Verify

```bash
# Check if secret was created from 1Password
kubectl get secret apc-ups-main -n infrastructure

# Check if configmap was created
kubectl get configmap certwarden-apc-scripts -n infrastructure
```

### 6. Configure Certwarden

Add to your Certwarden certificate configuration:

```yaml
postProcessing:
  - name: deploy-to-apc
    script: /path/to/certwarden-apc-deploy.sh
    environment:
      - name: APC_HOST
        value: "ups-main"  # Matches secret pattern: apc-{APC_HOST}
      - name: NAMESPACE
        value: "infrastructure"
```

## Testing

Trigger a certificate renewal in Certwarden and watch the deployment:

```bash
# Watch jobs being created
kubectl get jobs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy -w

# View logs from the deployment
kubectl logs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy --tail=50
```

## Troubleshooting

### Job fails with "connection refused"

- Check that SSH is enabled on the APC NMC
- Verify the hostname/IP is correct and reachable from the cluster

### Job fails with "fingerprint mismatch"

- Re-run the ssh-keyscan command and update the fingerprint in 1Password

### Job fails with "authentication failed"

- Verify the username and password in 1Password
- Try logging into the NMC web interface to confirm credentials

### View detailed logs

```bash
# Get the most recent job
JOB=$(kubectl get jobs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}')

# View full logs
kubectl logs -n infrastructure job/$JOB
```

## Next Steps

- Add additional APC devices by creating more ExternalSecret resources
- Configure certificate auto-renewal schedules in Certwarden
- Set up monitoring for certificate expiration

For detailed documentation, see [README.md](./README.md).
