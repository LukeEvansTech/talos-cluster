# Known Issues

This document tracks known issues and their workarounds in the cluster.

## 1Password Connect PushSecret False 400 Errors

### Issue

PushSecret resources generate spurious HTTP 400 errors in logs despite successfully syncing secrets to 1Password:

```
Warning: Errored
set secret failed: could not write remote ref tls.key to target secretstore onepassword:
error updating 1Password Item: status 400: Unable to update item "codelooks-com-production-tls"
in Vault "w7oprzm4euz5yajs6gnje7bpzu"
```

However, checking the PushSecret status shows it's actually working:

```bash
$ kubectl get pushsecret codelooks-com-production-tls -n cert-manager
NAME                           AGE   STATUS
codelooks-com-production-tls   90d   Synced
```

### Root Cause

This is a **known bug** in 1Password Connect starting from version 1.7.3+:
- [External Secrets Issue #3631](https://github.com/external-secrets/external-secrets/issues/3631)
- 1Password Connect returns HTTP 400 errors even when updates succeed
- The External Secrets Operator correctly reports the PushSecret as `Synced: True`
- The errors are cosmetic noise from 1Password Connect itself

### Affected Versions

- **1Password Connect**: 1.7.3+ (including 1.8.1 currently deployed)
- **External Secrets Operator**: 0.9.19+
- **Working version**: 1Password Connect 1.15.0

### Current Status

- Secrets ARE syncing successfully to 1Password
- The 400 errors are false positives and can be safely ignored
- No functional impact on cert-manager or PushSecret operations

### Workarounds

#### Option 1: Ignore the Errors (Recommended)
The errors are harmless. Verify PushSecret is working:

```bash
kubectl get pushsecret -A
kubectl describe pushsecret <name> -n <namespace> | grep -A 5 "Status:"
```

If status shows `Synced: True`, the secret is successfully pushed to 1Password.

#### Option 2: Downgrade 1Password Connect
Downgrade to the last known working version:

```yaml
# kubernetes/apps/external-secrets/onepassword/app/helmrelease.yaml
spec:
  values:
    connect:
      api:
        image:
          repository: ghcr.io/1password/connect-api
          tag: 1.15.0
      sync:
        image:
          repository: ghcr.io/1password/connect-sync
          tag: 1.15.0
```

#### Option 3: Restart 1Password Connect Periodically
Temporary workaround that clears errors for a few days:

```bash
kubectl rollout restart deployment onepassword -n external-secrets
```

### Verification

To verify secrets are actually syncing to 1Password:

1. Check PushSecret status:
   ```bash
   kubectl describe pushsecret <name> -n <namespace>
   ```

2. Look for `Synced Push Secrets` section showing successful sync

3. Verify in 1Password vault that the item exists and contains current data

4. Check External Secrets Operator logs for actual errors vs. noise:
   ```bash
   kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=50
   ```

### Example Configuration

Working PushSecret configuration for cert-manager TLS certificates:

```yaml
---
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
  name: &name "${SECRET_DOMAIN/./-}-production-tls"
spec:
  secretStoreRefs:
    - name: onepassword
      kind: ClusterSecretStore
  selector:
    secret:
      name: *name
  template:
    engineVersion: v2
    data:
      tls.crt: '{{ index . "tls.crt" | b64enc }}'
      tls.key: '{{ index . "tls.key" | b64enc }}'
  data:
    - match:
        secretKey: &key tls.crt
        remoteRef:
          remoteKey: *name
          property: *key
    - match:
        secretKey: &key tls.key
        remoteRef:
          remoteKey: *name
          property: *key
```

### References

- [External Secrets GitHub Issue #3631](https://github.com/external-secrets/external-secrets/issues/3631)
- [1Password Connect Release Notes](https://app-updates.agilebits.com/product_history/Connect)
- [External Secrets PushSecret Documentation](https://external-secrets.io/latest/api/pushsecret/)

### Resolution Status

⏳ **Pending upstream fix** - Monitoring issue #3631 for resolution from 1Password Connect team.

---

**Last Updated**: 2025-11-13
**Cluster**: talos-cluster
