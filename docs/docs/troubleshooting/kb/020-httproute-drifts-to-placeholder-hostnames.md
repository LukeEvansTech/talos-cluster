# KB-020: App Returns 404 Through the Gateway (HTTPRoute Drifted to Placeholder Hostnames)

**Status:** Structurally fixed 2026-08-03: the placeholder Secret now carries
`kustomize.toolkit.fluxcd.io/ssa: IfNotPresent`, so the race below can no longer recur.
Objects that drifted *before* the fix stay drifted until remediated per-app (see Fix).

## Symptom

An app returns the cluster's custom `error-pages` **404** when reached through the envoy
gateway at its real `<app>.${SECRET_DOMAIN}` hostname, even though the pod and HelmRelease are
healthy. The app answers **only** on its direct pod/Service IP. Forcing a helm upgrade does
**not** fix it.

## Cause

The **live** HTTPRoute's `spec.hostnames` are the placeholder defaults `<app>.example.com` /
`<app>.internal.example.com` instead of the real domain. No route then claims
`<app>.${SECRET_DOMAIN}`, so the request falls through to the `*` catch-all route and gets the
custom 404.

Where the placeholder comes from is the **first-render substitution race**:

- `kubernetes/components/global-vars/` is a Component included by every namespace. It ships
  **both** a placeholder `cluster-secrets` Secret (fake `SECRET_DOMAIN: "example.com"`, fake
  CIDRs/paths, needed for offline rendering, both `flate` locally and Konflate's in-cluster PR
  renders) **and** the real `cluster-secrets` ExternalSecret (`creationPolicy: Owner`,
  1Password).
- The placeholder applies **instantly**; ESO overwrites it seconds later. Any app whose
  Kustomization runs PostBuild substitution **in that window** bakes the *placeholder* value of
  `${SECRET_DOMAIN}` (or any `${VAR}`) into its helm-rendered objects.

Why a forced upgrade won't self-heal it:

- The HelmRelease `values` are **correct** (`{{ .Release.Name }}.${SECRET_DOMAIN}`), and helm's
  rendered + stored manifest is **correct**.
- But helm only patches resources that **differ between consecutive revisions**. Every recent
  revision renders the real domain identically, so helm computes **no diff** for the route and
  leaves the drifted live object untouched. `flux reconcile hr --force` bumps the release but
  still won't touch the un-diffed route. (helm-controller `driftDetection` is intentionally
  **off**: enabling it would fight the zeroscaler HPAs on
  `spec.replicas`.)

This is **not** domain-specific: the domain/route is just the most visible victim of the
placeholder-then-ESO race; any `${VAR}` rendered in that window can be caught.

## Fix

Per affected app (no Git change, the HelmRelease is already correct):

```sh
kubectl delete httproute -n <ns> <app>
flux reconcile hr <app> -n <ns> --force          # helm recreates the route from the correct manifest
kubectl get httproute -n <ns> <app> -o jsonpath='{.spec.hostnames}'   # verify the real domain
```

Deleting makes the resource **missing**, so the forced upgrade recreates it with the rendered
(correct) hostnames.

Find every affected route:

```sh
kubectl get httproute -A -o json | jq -r '.items[]
  | select(any(.spec.hostnames[]?; test("example\\.com")))
  | "\(.metadata.namespace)/\(.metadata.name)"'
```

## How to recognise fast

- Verify **end-to-end through the gateway with real SNI**. A bare `Host:` header gives a
  misleading `200` from the catch-all:

  ```sh
  curl -sk --resolve <app>.${SECRET_DOMAIN}:443:<envoy-internal-ip> https://<app>.${SECRET_DOMAIN}/
  ```

  and confirm the body is **not** `Error 404: Not Found`.

## Structural fix (shipped 2026-08-03)

The race was originally left in place as "rare, first render only". That aged badly: the
placeholder was not applied once at first render, it was re-applied on **every**
kustomize-controller reconcile (managedFields showed kustomize-controller writing the
placeholder and ESO overwriting it one second later, hourly). Each reconcile reopened the
window, and on 2026-07-30 the giteamirror route rendered `example.com` mid-reconcile (the
helm upgrade that wrote it died with "context canceled") and stayed drifted for four days.

The fix is one annotation on the placeholder Secret in
`kubernetes/components/global-vars/cluster-secrets.yaml`:

```yaml
kustomize.toolkit.fluxcd.io/ssa: IfNotPresent
```

kustomize-controller now creates the Secret only when it does not exist (bootstrap), and
never again overwrites the ESO-managed real values. CI rendering (flate/Konflate) is
unaffected: both read the git file, not the live object. Trade-off: a key added to the
placeholder file after bootstrap reaches only CI rendering; the live value must land in
the `cluster-secrets` 1Password item, which was already the required workflow.

Options considered earlier and declined: global `driftDetection.mode: enabled` (would
fight the zeroscaler HPAs), a self-healing CronJob guard, splitting `cluster-secrets` into
its own `dependsOn`-gated Kustomization (complete but invasive), and hardcoding the domain
literally (only patches the domain symptom, not the general race).

## References

- Gateway API HTTPRoute: <https://gateway-api.sigs.k8s.io/api-types/httproute/>
- Related: [KB-015](015-slow-image-pulls-exceed-helmrelease-timeout.md) (other helm
  remediation/upgrade pitfalls).
