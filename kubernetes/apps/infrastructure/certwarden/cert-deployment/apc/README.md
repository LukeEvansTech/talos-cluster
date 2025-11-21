# Certwarden APC NMC Certificate Deployment

Automated certificate deployment for APC Network Management Cards (NMC2 & NMC3) using [apc-p15-tool](https://github.com/gregtwallace/apc-p15-tool).

## Overview

This setup allows Certwarden to automatically deploy SSL certificates to APC UPS devices with Network Management Cards after certificate renewal. It uses Kubernetes Jobs to execute the deployment and pulls credentials from 1Password via External Secrets.

## Architecture

1. **Certwarden** triggers the `certwarden-apc-deploy.sh` script after certificate renewal
2. The script creates a **Kubernetes Job** that:
   - Downloads the `apc-p15-tool` binary
   - Reads APC credentials from a Kubernetes Secret (populated by External Secrets)
   - Runs `apc-updater.py` which calls `apc-p15-tool install` to deploy the certificate
3. The certificate is installed on the APC NMC device via SSH
4. The Job automatically cleans up after 5 minutes

## Prerequisites

- APC UPS with NMC2 or NMC3 network management card
- SSH access enabled on the APC NMC
- Admin credentials for the APC NMC
- SSH host key fingerprint of the APC NMC
- 1Password item with APC credentials
- Certwarden configured for certificate management

## Setup

### 1. Get APC NMC SSH Fingerprint

First, you need to get the SSH host key fingerprint of your APC NMC:

```bash
ssh-keyscan -H <apc-hostname> | ssh-keygen -lf -
```

Example output:
```
256 SHA256:ABC123def456ghi789... (ED25519)
```

Use the part after `SHA256:` as your fingerprint value.

### 2. Create 1Password Item

Create a new item in 1Password with the following fields:

- **Item name**: `apc-ups-main` (or your chosen identifier)
- **Fields**:
  - `APC_HOSTNAME`: Hostname or IP address (e.g., `ups.example.com`)
  - `APC_USERNAME`: Admin username (typically `apc`)
  - `APC_PASSWORD`: Password for the NMC
  - `APC_FINGERPRINT`: SSH fingerprint from step 1

### 3. Update External Secret

Edit `externalsecret.yaml` to match your 1Password item name:

```yaml
metadata:
  name: apc-ups-main  # Match your item name
spec:
  target:
    name: apc-ups-main  # Match your item name
  dataFrom:
    - extract:
        key: apc-ups-main  # Your 1Password item name
```

### 4. Deploy to Kubernetes

```bash
kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/apc/
```

This will create:
- ExternalSecret to pull credentials from 1Password
- ConfigMap with the deployment scripts
- Required RBAC permissions (if not already present from IPMI setup)

### 5. Configure Certwarden

In your Certwarden certificate configuration, add a post-processing script:

```yaml
apiVersion: certwarden.io/v1alpha1
kind: Certificate
metadata:
  name: apc-ssl-cert
  namespace: infrastructure
spec:
  # ... certificate configuration ...
  postProcessing:
    - name: deploy-to-apc
      script: /path/to/certwarden-apc-deploy.sh
      environment:
        - name: APC_HOST
          value: "ups-main"  # Must match secret name prefix (apc-{APC_HOST})
        - name: NAMESPACE
          value: "infrastructure"
```

**Important**: The `APC_HOST` value must match the secret name pattern `apc-{APC_HOST}`. For example:
- If your secret is `apc-ups-main`, use `APC_HOST=ups-main`
- If your secret is `apc-datacenter-ups`, use `APC_HOST=datacenter-ups`

## How It Works

### Certificate Deployment Flow

1. **Certwarden** renews a certificate and calls `certwarden-apc-deploy.sh`
2. The script receives certificate data via environment variables:
   - `CERTIFICATE_PEM`: The renewed certificate
   - `PRIVATE_KEY_PEM`: The private key
   - `APC_HOST`: Target APC identifier
3. Script creates a temporary secret with the certificate data
4. Script creates a Kubernetes Job that:
   - Uses `python:3.12-alpine` base image
   - Installs kubectl, openssh-client, and downloads apc-p15-tool
   - Reads APC credentials from the External Secret
   - Executes `apc-updater.py` to deploy the certificate
5. `apc-updater.py` calls `apc-p15-tool install` which:
   - Converts the PEM certificate/key to P15 format
   - Connects to the APC NMC via SSH
   - Uploads and installs the certificate
6. Job completes and auto-deletes after 5 minutes

### Files

- **apc-updater.py**: Python wrapper that calls apc-p15-tool
- **certwarden-apc-deploy.sh**: Bash script to create the deployment Job
- **externalsecret.yaml**: Pulls APC credentials from 1Password
- **kustomization.yaml**: Kustomize configuration to deploy everything

### Security

- Credentials are stored securely in 1Password
- Temporary certificate secrets are automatically cleaned up
- Jobs are automatically deleted after 5 minutes
- SSH fingerprint verification prevents MITM attacks
- RBAC limits permissions to only what's needed

## Troubleshooting

### View Job Status

```bash
# List recent APC certificate deployment jobs
kubectl get jobs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy

# View logs from the most recent job
kubectl logs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy --tail=100
```

### Common Issues

#### SSH Connection Failures

- **Fingerprint mismatch**: Verify the fingerprint in 1Password matches the actual SSH host key
- **SSH not enabled**: Ensure SSH is enabled on the APC NMC (Console → Network → SSH)
- **Firewall**: Check that port 22 is accessible from your Kubernetes cluster

#### Authentication Failures

- **Wrong credentials**: Verify username/password in 1Password
- **Account locked**: Check if the admin account is locked due to failed login attempts
- **Insufficient permissions**: Ensure the user has admin privileges

#### Certificate Format Issues

- **Invalid key type**: Verify your key type is supported (RSA 1024-4092, ECDSA P-256/384/521)
- **Certificate chain**: The tool handles certificate chains automatically

#### Job Failures

```bash
# Describe the job for events
kubectl describe job <job-name> -n infrastructure

# Check pod logs for detailed error messages
kubectl logs -n infrastructure <pod-name>
```

### Debug Mode

The scripts run with `--debug` flag by default. Check the Job logs for detailed output:

```bash
kubectl logs -n infrastructure -l app.kubernetes.io/name=certwarden-apc-deploy
```

## Supported Devices

- APC UPS with NMC2 (Network Management Card 2)
- APC UPS with NMC3 (Network Management Card 3)

Tested models include:
- APC Smart-UPS series
- APC Symmetra series
- Any APC device with compatible NMC

## References

- [apc-p15-tool GitHub Repository](https://github.com/gregtwallace/apc-p15-tool)
- [Certwarden Documentation](https://github.com/gregtwallace/certwarden)
- [APC Network Management Card Documentation](https://www.apc.com/)

## License

This implementation follows the GPL v2 license to maintain compatibility with the underlying tools.
