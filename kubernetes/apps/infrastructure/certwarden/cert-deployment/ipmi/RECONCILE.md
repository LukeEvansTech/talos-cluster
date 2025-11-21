# Reconciling and Monitoring Deployment

All changes have been committed and pushed. Use these commands to reconcile and monitor the deployment.

## Prerequisites

Ensure you have kubectl access to your talos cluster and flux CLI installed.

## Step 1: Reconcile Flux GitRepository

Force Flux to pull the latest changes from git:

```bash
# Reconcile the git source
flux reconcile source git flux-system -n flux-system

# Expected output:
# ► annotating GitRepository flux-system in flux-system namespace
# ✔ GitRepository annotated
# ◐ waiting for GitRepository reconciliation
# ✔ fetched revision main@sha1:010a9d40...
```

## Step 2: Reconcile Certwarden Kustomization

Apply the cert-deployment manifests:

```bash
# Reconcile certwarden kustomization
flux reconcile kustomization certwarden -n flux-system

# Expected output:
# ► annotating Kustomization certwarden in flux-system namespace
# ✔ Kustomization annotated
# ◐ waiting for Kustomization reconciliation
# ✔ applied revision main@sha1:010a9d40...
```

## Step 3: Verify Resources Created

Check that the ExternalSecret and ConfigMap were created:

```bash
# Check ExternalSecret
kubectl get externalsecret -n infrastructure ipmi-credentials

# Expected output:
# NAME               STORE         REFRESH INTERVAL   STATUS   READY
# ipmi-credentials   onepassword   1h                 Ready    True

# Check that secret was created
kubectl get secret -n infrastructure ipmi-credentials

# Expected output:
# NAME               TYPE     DATA   AGE
# ipmi-credentials   Opaque   1      30s

# Check ConfigMap
kubectl get configmap -n infrastructure ipmi-hosts-config

# Expected output:
# NAME                DATA   AGE
# ipmi-hosts-config   1      30s
```

## Step 4: Verify Secret Contents (Optional)

Verify the IPMI password was pulled from 1Password:

```bash
# Check secret keys (don't show values)
kubectl get secret -n infrastructure ipmi-credentials -o jsonpath='{.data}' | jq 'keys'

# Expected output:
# [
#   "IPMI_TESTHOST_PASSWORD"
# ]

# If you need to verify the value (be careful - this exposes the password)
# kubectl get secret -n infrastructure ipmi-credentials -o jsonpath='{.data.IPMI_TESTHOST_PASSWORD}' | base64 -d
```

## Step 5: Verify ConfigMap Contents

```bash
# Check ConfigMap data
kubectl get configmap -n infrastructure ipmi-hosts-config -o yaml

# Expected to see:
# data:
#   testhost: "https://ipmi-test.example.com,X12,ADMIN,IPMI_TESTHOST_PASSWORD"
```

## Step 6: Monitor Flux Events

Watch for any issues during reconciliation:

```bash
# Watch flux events
flux events -n flux-system --for Kustomization/certwarden

# Or use kubectl
kubectl get events -n infrastructure --sort-by='.lastTimestamp' | tail -20
```

## Troubleshooting

### ExternalSecret Not Ready

If ExternalSecret shows `Status: SecretSyncedError`:

```bash
# Check ExternalSecret details
kubectl describe externalsecret -n infrastructure ipmi-credentials

# Common issues:
# 1. Item "ipmi-credentials" not found in 1Password
# 2. Field "IPMI_TESTHOST_PASSWORD" not in 1Password item
# 3. ClusterSecretStore "onepassword" not configured
```

**Fix**: Ensure the 1Password item exists with the correct field name.

### Secret Not Created

```bash
# Check ClusterSecretStore status
kubectl get clustersecretstore onepassword

# Should show: READY=True

# If not ready, check operator logs
kubectl logs -n external-secrets deploy/external-secrets -f
```

### Kustomization Fails to Apply

```bash
# Check kustomization status
kubectl get kustomization -n flux-system certwarden -o yaml

# Look for status.conditions with type: Ready, status: False
```

## Success Criteria

All checks should pass:

- ✅ GitRepository reconciled
- ✅ Kustomization reconciled
- ✅ ExternalSecret status: Ready=True
- ✅ Secret `ipmi-credentials` exists with password data
- ✅ ConfigMap `ipmi-hosts-config` exists with host configuration
- ✅ No error events in namespace

## What's Deployed

After successful reconciliation, you'll have:

```
infrastructure namespace:
├── Secret: ipmi-credentials (from 1Password)
│   └── IPMI_TESTHOST_PASSWORD
└── ConfigMap: ipmi-hosts-config
    └── testhost configuration
```

**NOT deployed yet** (manual):
- Job templates (you'll create these manually for testing)
- CronJob (disabled by default with `suspend: true`)

## Next Steps

Once all resources are ready:

1. **Update ConfigMap** with your actual test IPMI host details
2. **Update 1Password** with the actual IPMI password
3. **Follow QUICKSTART.md** to deploy a certificate to your test host

## Quick Status Check

One-liner to check everything:

```bash
echo "=== ExternalSecret ===" && \
kubectl get externalsecret -n infrastructure ipmi-credentials && \
echo -e "\n=== Secret ===" && \
kubectl get secret -n infrastructure ipmi-credentials && \
echo -e "\n=== ConfigMap ===" && \
kubectl get configmap -n infrastructure ipmi-hosts-config && \
echo -e "\n✅ All resources present!"
```

## Manual Apply (If Flux Not Available)

If you need to apply manually without waiting for Flux:

```bash
# Apply via kustomize
kubectl apply -k /path/to/talos-cluster/kubernetes/apps/infrastructure/certwarden/cert-deployment/

# Or apply individually
kubectl apply -f externalsecret.yaml
kubectl apply -f configmap.yaml
```
