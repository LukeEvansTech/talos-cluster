# IPMI Certificate Deployment for Certwarden

Automated certificate deployment to Supermicro IPMI interfaces using the `supermicro-ipmi-cert` container.

## Overview

This setup allows Certwarden to automatically deploy certificates to Supermicro IPMI interfaces (X9, X10, X11, X12, X13, H13) using Kubernetes Jobs.

## Architecture

```
┌─────────────┐
│ Certwarden  │
│   (Cert     │
│  Manager)   │
└──────┬──────┘
       │ Certificate Renewed
       │
       ┌──────┴───────────────────────┐
       │                              │
┌──────▼──────┐              ┌────────▼──────┐
│ Manual Job  │              │   CronJob     │
│  (Testing)  │              │  (Periodic)   │
└──────┬──────┘              └────────┬──────┘
       │                              │
       └──────────┬───────────────────┘
                  │
         ┌────────▼────────┐
         │  Deploy Cert    │
         │   Container     │
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │  IPMI Interface │
         │  (X9-X13, H13)  │
         └─────────────────┘
```

## Prerequisites

1. **1Password** - For storing IPMI credentials
2. **ExternalSecrets** - Already configured in your cluster
3. **Certwarden** - Already deployed
4. **Container Image** - `ghcr.io/lukeevanstech/supermicro-ipmi-cert:latest`

## Setup Instructions

### Step 1: Store IPMI Credentials in 1Password

Create an item in 1Password with your IPMI credentials:

1. **Item Name**: `ipmi-credentials`
2. **Vault**: Your cluster vault
3. **Fields**:
   ```
   IPMI_TESTHOST_PASSWORD=your-ipmi-password-here
   IPMI_HOST1_PASSWORD=another-password
   IPMI_HOST2_PASSWORD=yet-another-password
   ```

### Step 2: Configure IPMI Hosts

Edit `configmap.yaml` and add your IPMI hosts:

```yaml
data:
  # Format: hostname=URL,MODEL,USERNAME,PASSWORD_ENV_VAR
  testhost: "https://192.168.1.100,X12,ADMIN,IPMI_TESTHOST_PASSWORD"
  host1: "https://ipmi-host1.local,X11,ADMIN,IPMI_HOST1_PASSWORD"
  host2: "https://ipmi-host2.local,X12,root,IPMI_HOST2_PASSWORD"
```

**Supported Models**: X9, X10, X11, X12, X13, H13

### Step 3: Update ExternalSecret

Edit `externalsecret.yaml` to match your 1Password item:

```yaml
data:
  IPMI_TESTHOST_PASSWORD: "{{ .IPMI_TESTHOST_PASSWORD }}"
  IPMI_HOST1_PASSWORD: "{{ .IPMI_HOST1_PASSWORD }}"
  IPMI_HOST2_PASSWORD: "{{ .IPMI_HOST2_PASSWORD }}"
```

### Step 4: Apply the Manifests

```bash
cd /path/to/talos-cluster
kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/
```

Verify secrets were created:
```bash
kubectl get secret -n infrastructure ipmi-credentials
kubectl get configmap -n infrastructure ipmi-hosts-config
```

## Testing with Manual Job

### Option A: Using a Test Certificate

First, get a test certificate from Certwarden or create a self-signed cert:

```bash
# Generate test certificate (for testing only)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout test-key.pem -out test-cert.pem \
  -days 365 -subj "/CN=test.example.com"
```

### Option B: Use Existing Certificate

Get certificate from your Certwarden:

```bash
# Export from Certwarden (or use Certwarden API)
kubectl exec -n infrastructure deploy/certwarden -- \
  cat /app/data/certificates/your-cert/fullchain.pem > cert.pem

kubectl exec -n infrastructure deploy/certwarden -- \
  cat /app/data/certificates/your-cert/privkey.pem > key.pem
```

### Create and Run the Job

Create a test job file:

```bash
cat > test-deploy-job.yaml <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: deploy-cert-ipmi-testhost
  namespace: infrastructure
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
      - name: deploy-cert
        image: ghcr.io/lukeevanstech/supermicro-ipmi-cert:latest
        env:
        # CONFIGURE YOUR IPMI HOST HERE
        - name: IPMI_URL
          value: "https://192.168.1.100"  # YOUR IPMI IP
        - name: IPMI_MODEL
          value: "X12"  # YOUR MODEL
        - name: IPMI_USERNAME
          value: "ADMIN"  # YOUR USERNAME
        - name: IPMI_PASSWORD_ENV
          value: "IPMI_TESTHOST_PASSWORD"
        - name: IPMI_NO_REBOOT
          value: "false"
        # Certificate data
        - name: CERTIFICATE_PEM
          value: |
$(cat cert.pem | sed 's/^/            /')
        - name: PRIVATE_KEY_PEM
          value: |
$(cat key.pem | sed 's/^/            /')
        envFrom:
        - secretRef:
            name: ipmi-credentials
        resources:
          requests:
            cpu: 100m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 128Mi
EOF

# Apply the job
kubectl apply -f test-deploy-job.yaml
```

### Monitor the Job

```bash
# Watch job status
kubectl get jobs -n infrastructure -w

# View logs
kubectl logs -n infrastructure job/deploy-cert-ipmi-testhost -f

# Check job details
kubectl describe job -n infrastructure deploy-cert-ipmi-testhost
```

**Expected Output:**
```
2025-11-20 ... - INFO - === Supermicro IPMI Certificate Deployment ===
2025-11-20 ... - INFO - Target: https://192.168.1.100
2025-11-20 ... - INFO - Model: X12
2025-11-20 ... - INFO - Logging into IPMI...
2025-11-20 ... - INFO - Login successful
2025-11-20 ... - INFO - Certificate uploaded successfully
2025-11-20 ... - INFO - IPMI reboot initiated
2025-11-20 ... - INFO - === Deployment completed successfully ===
```

### Troubleshooting Test Deployment

**Login Failed:**
- Check IPMI URL is correct and accessible from cluster
- Verify username/password in 1Password secret
- Ensure IPMI web interface is enabled
- Check network connectivity: `kubectl run -it --rm debug --image=alpine -- ping <IPMI_IP>`

**Certificate Upload Failed:**
- Verify model is correct (X9/X10/X11/X12/X13/H13)
- Check certificate format (PEM)
- Ensure certificate and key match

**Timeout:**
- IPMI might be slow to respond
- Check if IPMI is accessible from cluster network
- Try increasing timeout in container code

## Integration with Certwarden

### Approach 1: Certwarden API Client (Recommended)

Create a sidecar container that watches Certwarden's API and triggers deployments:

```yaml
# Add to certwarden helmrelease.yaml
controllers:
  certwarden:
    containers:
      cert-deployer:
        image: ghcr.io/lukeevanstech/certwarden-ipmi-deployer:latest  # Future work
        env:
          - name: CERTWARDEN_URL
            value: "http://localhost:4050"
          - name: IPMI_HOSTS_CONFIG
            valueFrom:
              configMapKeyRef:
                name: ipmi-hosts-config
                key: testhost
```

### Approach 2: CronJob (Simple but Delayed)

Enable the CronJob for periodic deployment:

```bash
# Edit cronjob.yaml and set suspend: false
kubectl edit cronjob -n infrastructure deploy-cert-ipmi-testhost

# Or enable via kustomization:
# Uncomment "- ./cronjob.yaml" in kustomization.yaml
```

**Pros**: Simple, no modifications to Certwarden
**Cons**: Not immediate, runs on schedule

### Approach 3: Webhook Receiver (Advanced)

Deploy a webhook receiver that Certwarden can call:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cert-webhook-receiver
spec:
  # Webhook receiver that creates Jobs on POST
  # See: kubernetes/apps/infrastructure/certwarden/webhook-receiver/
  # (Future implementation)
```

## Adding More IPMI Hosts

1. **Add credential to 1Password**:
   ```
   IPMI_NEWHOST_PASSWORD=password-here
   ```

2. **Update ExternalSecret**:
   ```yaml
   data:
     IPMI_NEWHOST_PASSWORD: "{{ .IPMI_NEWHOST_PASSWORD }}"
   ```

3. **Update ConfigMap**:
   ```yaml
   data:
     newhost: "https://ipmi-new.local,X12,ADMIN,IPMI_NEWHOST_PASSWORD"
   ```

4. **Create Job for new host** (duplicate and modify job-template.yaml)

5. **Apply changes**:
   ```bash
   kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/
   ```

## Security Considerations

- ✅ Runs as non-root user (UID 1000)
- ✅ Read-only root filesystem
- ✅ Dropped all capabilities
- ✅ Secrets stored in 1Password/ExternalSecrets
- ✅ No certificate data persisted to disk
- ⚠️ SSL verification disabled for IPMI connections (required for self-signed IPMI certs)

## Next Steps

1. **Test with your IPMI host** - Use the manual job approach
2. **Automate with Certwarden** - Integrate using one of the approaches above
3. **Add more hosts** - Follow the "Adding More IPMI Hosts" section
4. **Monitor** - Set up alerts for failed deployments

## Files in This Directory

```
cert-deployment/
├── README.md              # This file
├── kustomization.yaml     # Kustomize config
├── externalsecret.yaml    # IPMI credentials from 1Password
├── configmap.yaml         # IPMI host configurations
├── job-template.yaml      # Template for manual testing
└── cronjob.yaml          # Periodic deployment (optional)
```

## Support

- **Container Issues**: https://github.com/LukeEvansTech/containers/issues
- **Certwarden Docs**: https://www.certwarden.com/docs/

## Related Documentation

- [Certwarden Post-Processing](https://www.certwarden.com/docs/using_certificates/post_process_bin/)
- [supermicro-ipmi-cert Container](https://github.com/LukeEvansTech/containers/tree/main/apps/supermicro-ipmi-cert)
