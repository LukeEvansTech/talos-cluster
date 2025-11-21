# Quick Start: Deploy Certificate to Test IPMI Host

Follow these steps to quickly test certificate deployment to your IPMI host.

## 1. Prerequisites

- ✅ IPMI host accessible from Kubernetes cluster
- ✅ IPMI credentials (username/password)
- ✅ Certificate and private key files (PEM format)

## 2. Store IPMI Password in 1Password

Create item `ipmi-credentials` in 1Password:
```
IPMI_TESTHOST_PASSWORD=your-actual-password
```

## 3. Quick Deploy

```bash
# Navigate to talos-cluster repo
cd /path/to/talos-cluster

# Set your IPMI details
export IPMI_URL="https://192.168.1.100"  # Your IPMI IP
export IPMI_MODEL="X12"                   # Your model (X9/X10/X11/X12/X13/H13)
export IPMI_USERNAME="ADMIN"             # Your username

# Apply the ExternalSecret and ConfigMap
kubectl apply -k kubernetes/apps/infrastructure/certwarden/cert-deployment/

# Wait for secret to be created
kubectl wait --for=condition=Ready externalsecret/ipmi-credentials -n infrastructure --timeout=60s

# Create test certificate (or use your own cert.pem and key.pem)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem \
  -days 365 -subj "/CN=test.example.com"

# Create the deployment job
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: deploy-cert-ipmi-test
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
        - name: IPMI_URL
          value: "${IPMI_URL}"
        - name: IPMI_MODEL
          value: "${IPMI_MODEL}"
        - name: IPMI_USERNAME
          value: "${IPMI_USERNAME}"
        - name: IPMI_PASSWORD_ENV
          value: "IPMI_TESTHOST_PASSWORD"
        - name: IPMI_NO_REBOOT
          value: "false"
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

# Watch the job
kubectl logs -n infrastructure job/deploy-cert-ipmi-test -f
```

## 4. Expected Output

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

## 5. Verify

Access your IPMI web interface and check:
1. Certificate is installed
2. Valid dates match your certificate
3. No browser warnings (if using proper domain cert)

## 6. Cleanup

```bash
# Job auto-deletes after 5 minutes (ttlSecondsAfterFinished: 300)
# Or manually delete:
kubectl delete job -n infrastructure deploy-cert-ipmi-test

# Remove test certificates
rm -f cert.pem key.pem
```

## Common Issues

### "Login failed"
- Check IPMI URL is accessible: `kubectl run -it --rm debug --image=alpine -- ping YOUR_IPMI_IP`
- Verify password in 1Password
- Confirm username is correct (usually `ADMIN` or `root`)

### "Connection timeout"
- IPMI might be firewalled from cluster
- Check IPMI is on accessible network segment
- Verify IPMI web interface is enabled

### "Certificate upload failed"
- Verify model is correct for your hardware
- Check certificate format is PEM
- Ensure certificate and key match

## Next Steps

✅ **Test passed?** Great! Now configure Certwarden integration (see README.md)

❌ **Test failed?** Check the troubleshooting section in README.md or open an issue.
