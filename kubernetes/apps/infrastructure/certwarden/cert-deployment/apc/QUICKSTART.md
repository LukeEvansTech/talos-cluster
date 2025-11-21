# APC NMC Certificate Deployment - Quick Start

Get your APC UPS certificate deployment up and running in 5 minutes.

## Prerequisites

- APC UPS with NMC2 or NMC3 network card
- SSH access to the APC NMC
- Admin credentials
- 1Password with External Secrets configured in your cluster

## Quick Setup

### 1. Get SSH Fingerprint

For APC devices (which use cryptlib SSH), use this command:

```bash
ssh -o KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    -v apc@10.32.8.58 exit 2>&1 | grep "Server host key"
```

Example output:
```
debug1: Server host key: ssh-rsa SHA256:4sd7MpvwhQrOEhAIjlL5Cr2s6ml0c22KX0rxYClwbN8
```

Copy the fingerprint (the part after `SHA256:`).

### 2. Create 1Password Item

Create a new item named `apc-ups-main` with these fields:

| Field Name          | Example Value                            |
|---------------------|------------------------------------------|
| APC_HOSTNAME        | 10.32.8.58                               |
| APC_USERNAME        | apc                                      |
| APC_PASSWORD        | your-password                            |
| APC_FINGERPRINT     | 4sd7MpvwhQrOEhAIjlL5Cr2s6ml0c22KX0rxYClwbN8 |
| APC_INSECURE_CIPHER | true                                     |

**Note**: Set `APC_INSECURE_CIPHER` to `true` for older APC devices that use legacy SSH ciphers (cryptlib).

### 3. Edit External Secret (if needed)

Edit `externalsecret.yaml` if your 1Password item has a different name:

```yaml
metadata:
  name: apc-ups-main  # Change to match your 1Password item
spec:
  target:
    name: apc-ups-main  # Change to match your 1Password item
  dataFrom:
    - extract:
        key: apc-ups-main  # Your 1Password item name
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

- Check that SSH is enabled on the APC NMC (Console → Network → SSH)
- Verify the hostname/IP is correct and reachable from the cluster

### Job fails with "fingerprint mismatch"

- Re-run the SSH fingerprint command and update in 1Password

### Job fails with "authentication failed"

- Verify the username and password in 1Password
- Try logging into the NMC web interface to confirm credentials

### Job fails with SSH cipher errors

- Make sure `APC_INSECURE_CIPHER` is set to `true` in 1Password
- This is required for older APC devices with cryptlib SSH

### View detailed logs

```bash
# Get the most recent job
JOB=$(kubectl get jobs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}')

# View full logs
kubectl logs -n infrastructure job/$JOB
```

## Your Specific Configuration

For your APC device at `10.32.8.58`:

- **Fingerprint**: `4sd7MpvwhQrOEhAIjlL5Cr2s6ml0c22KX0rxYClwbN8`
- **Insecure Cipher**: Required (set to `true`)
- Device uses cryptlib SSH server (legacy)

## Next Steps

- Add additional APC devices by creating more ExternalSecret resources
- Configure certificate auto-renewal schedules in Certwarden
- Set up monitoring for certificate expiration
