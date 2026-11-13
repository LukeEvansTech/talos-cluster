# KB-209: Webhooks time out for seconds after a network policy change, and Flux Kustomizations stay failed

**Status:** Self-healing. The webhook recovers in seconds and Flux retries failed Kustomizations
every 2 minutes (`retryInterval`); a manual reconcile only speeds that up. Avoidable by ordering the
change.

## Symptom

Within a minute of merging a change that enforces ingress policy in a namespace (adding
`components/netpol` or a first `CiliumNetworkPolicy`), dozens of Flux Kustomizations go
`Ready=False` with:

```text
ExternalSecret/<ns>/<name> dry-run failed (InternalError): Internal error occurred:
failed calling webhook "validate.externalsecret.external-secrets.io": ... context deadline exceeded
```

The kube-apiserver logs `Failed calling webhook, failing closed` for about 20 seconds, then stops.
The webhook answers again, but the Kustomizations keep the error until their next retry, and their
dependents report `dependency '<ns>/<name>' is not ready` until the chain catches up.

## Cause

A namespace's policies and the clusterwide `CiliumClusterwideNetworkPolicy` objects in
`kubernetes/apps/kube-system/cilium-policies/app/` belong to different Flux Kustomizations, and each
reconciles on its own. When one PR both enforces a namespace and widens a clusterwide allow to cover
it (for example, removing the namespace from the `allow-kube-apiserver` exclusion list), whichever
lands first switches the namespace to default-deny before the other's allows exist. Cilium drops silently, so the API server sees a webhook
timeout rather than a refusal.

Seen on 2026-11-13 (#7345) when the six system namespaces were first enforced: about 20 seconds of
webhook timeouts left 44 Kustomizations failed. The world-ingress narrowing (#7347) briefly denied
syslog the same way.

## Fix

- **Prevent it:** any policy that newly selects an endpoint switches that endpoint to default-deny,
  so a widened clusterwide allow enforces on its own; merging it first just moves the gap. Stage the
  change without enforcement instead: ship every new or widened policy with
  `enableDefaultDeny: {ingress: false}`, confirm the live objects carry every allow
  (`kubectl get cnp -A`, `kubectl get ccnp`), then remove that field in a second PR so enforcement starts in one step with every allow already in
  place.
- **Recover faster:** reconcile the failed Kustomizations instead of waiting for their retries:

  ```bash
  flux get ks -A --status-selector ready=false --no-header | awk '{print $1, $2}' |
    while read -r ns ks; do flux reconcile ks "$ks" -n "$ns"; done
  ```

  A second pass clears the dependents.

## How to recognise fast

- `failed calling webhook` errors in the kube-apiserver log are bunched in a window of seconds that
  starts at the policy merge, with none after.
- A server-side dry-run of the same object now succeeds:
  `kubectl get externalsecret -n <ns> <name> -o json | kubectl apply --dry-run=server -f -`.
- `just kube hubble-drops` during the window shows `POLICY_DENIED` into the webhook's namespace.

## References

- `just kube hubble-drops` and `just kube lb-probe` are the verification tools for any policy change.
- [Networking](../../architecture/networking.md) for how policy is laid out.
