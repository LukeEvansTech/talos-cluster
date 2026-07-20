# KB-020: App Returns 404 Through the Gateway (HTTPRoute Drifted to Placeholder Hostnames)

**Status:** Rare and self-correcting; fixed per-app with a one-liner. Structural fix
**deliberately declined** (see end). If it recurs, just remediate — don't re-investigate.

## Symptom

An app returns the cluster's custom `error-pages` **404** when reached through the envoy
gateway at its real `<app>.${SECRET_DOMAIN}` hostname — even though the pod and HelmRelease are
healthy. The app answers **only** on its direct pod/Service IP. Forcing a helm upgrade does
**not** fix it.

## Cause

The **live** HTTPRoute's `spec.hostnames` are the placeholder defaults `<app>.example.com` /
`<app>.internal.example.com` instead of the real domain. No route then claims
`<app>.${SECRET_DOMAIN}`, so the request falls through to the `*` catch-all route and gets the
custom 404.

Where the placeholder comes from — the **first-render substitution race**:

- `kubernetes/components/global-vars/` is a Component included by every namespace. It ships
  **both** a placeholder `cluster-secrets` Secret (fake `SECRET_DOMAIN: "example.com"`, fake
  CIDRs/paths — needed for offline rendering, both `flate` locally and Konflate's in-cluster PR
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
  **off** — enabling it would fight the zeroscaler HPAs on
  `spec.replicas`.)

This is **not** domain-specific — the domain/route is just the most visible victim of the
placeholder-then-ESO race; any `${VAR}` rendered in that window can be caught.

## Fix

Per affected app (no Git change — the HelmRelease is already correct):

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

- Verify **end-to-end through the gateway with real SNI** — a bare `Host:` header gives a
  misleading `200` from the catch-all:

  ```sh
  curl -sk --resolve <app>.${SECRET_DOMAIN}:443:<envoy-internal-ip> https://<app>.${SECRET_DOMAIN}/
  ```

  and confirm the body is **not** `Error 404: Not Found`.

## Why no structural fix

Decided not to fix structurally: the race is rare (a handful of apps in the cluster's lifetime,
only at first render before ESO syncs), well understood, and self-corrects in the HelmRelease
values — only already-rendered live objects stay drifted, and the delete+recreate one-liner
clears those. Options considered and declined: global `driftDetection.mode: enabled` (would
fight the zeroscaler HPAs), a self-healing CronJob guard, splitting `cluster-secrets` into its own
`dependsOn`-gated Kustomization (the only complete fix, too invasive), and hardcoding the domain
literally (only patches the domain symptom, not the general race).

## References

- Gateway API HTTPRoute: <https://gateway-api.sigs.k8s.io/api-types/httproute/>
- Related: [KB-015](015-slow-image-pulls-exceed-helmrelease-timeout.md) (other helm
  remediation/upgrade pitfalls).
