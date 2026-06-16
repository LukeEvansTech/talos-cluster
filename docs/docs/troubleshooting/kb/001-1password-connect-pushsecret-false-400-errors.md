# KB-001: 1Password Connect PushSecret False 400 Errors

**Status:** Pending upstream fix — monitoring [external-secrets#3631](https://github.com/external-secrets/external-secrets/issues/3631) for resolution from the 1Password Connect team.

## Symptom

PushSecret resources generate spurious HTTP 400 errors in logs despite successfully syncing secrets to 1Password:

```text
Warning: Errored
set secret failed: could not write remote ref tls.key to target secretstore onepassword-connect:
error updating 1Password Item: status 400: Unable to update item "<domain>-production-tls"
in Vault "<vault-id>"
```

However, checking the PushSecret status shows it is actually working:

```bash
kubectl get pushsecret <name> -n cert-manager
# NAME   AGE   STATUS
# <name> 90d   Synced
```

Affected versions:

- **1Password Connect**: 1.7.3+ (including 1.8.1)
- **External Secrets Operator**: 0.9.19+
- **Working version**: 1Password Connect 1.15.0

## Cause

This is a known bug in 1Password Connect starting from version 1.7.3+:

- 1Password Connect returns HTTP 400 errors even when updates succeed.
- The External Secrets Operator correctly reports the PushSecret as `Synced: True`.
- The errors are cosmetic noise from 1Password Connect itself.

There is no functional impact on cert-manager or PushSecret operations — secrets ARE syncing successfully to 1Password, and the 400 errors are false positives that can be safely ignored.

## Fix

### Option 1: Ignore the errors (recommended)

The errors are harmless. Verify the PushSecret is working:

```bash
kubectl get pushsecret -A
kubectl describe pushsecret <name> -n <namespace> | grep -A 5 "Status:"
```

If status shows `Synced: True`, the secret is successfully pushed to 1Password.

### Option 2: Downgrade 1Password Connect

Downgrade to the last known working version:

```yaml
# kubernetes/apps/external-secrets/onepassword-connect/app/helmrelease.yaml
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

### Option 3: Restart 1Password Connect periodically

Temporary workaround that clears errors for a few days:

```bash
kubectl rollout restart deployment onepassword-connect -n external-secrets
```

### Verification

To verify secrets are actually syncing to 1Password:

1. Check PushSecret status:

    ```bash
    kubectl describe pushsecret <name> -n <namespace>
    ```

2. Look for the `Synced Push Secrets` section showing successful sync.
3. Verify in the 1Password vault that the item exists and contains current data.
4. Check External Secrets Operator logs for actual errors vs. noise:

    ```bash
    kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=50
    ```

### Example configuration

Working PushSecret configuration for cert-manager TLS certificates:

```yaml
---
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
  name: &name "${SECRET_DOMAIN/./-}-production-tls"
spec:
  secretStoreRefs:
    - name: onepassword-connect
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

## References

- [External Secrets GitHub Issue #3631](https://github.com/external-secrets/external-secrets/issues/3631)
- [1Password Connect Release Notes](https://app-updates.agilebits.com/product_history/Connect)
- [External Secrets PushSecret Documentation](https://external-secrets.io/latest/api/pushsecret/)
