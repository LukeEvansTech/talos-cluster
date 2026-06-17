# konflate

Read-only **Flux PR rendered-diff** review UI ([home-operations/konflate](https://github.com/home-operations/konflate)).
Renders this repository's Flux config at each open PR's merge-base vs head and
shows the _rendered_ Kubernetes diff (blast radius, image changes, render
failures) — the impact a one-line HelmRelease bump actually has, which a
file-level diff hides.

Reached at `https://konflate.${SECRET_DOMAIN}` (envoy-internal).

## Current mode: anonymous

`LukeEvansTech/talos-cluster` is **public**, so konflate reviews PRs anonymously
(no forge token). Open PRs re-render on `config.refreshInterval` (15m).

## Optional upgrade: webhook-driven (instant PR refresh)

To refresh a PR the moment it's pushed instead of waiting up to 15m, add a
webhook secret (mirrors onedr0p/home-ops):

1. Create a 1Password item `konflate` with field `KONFLATE_WEBHOOK_SECRET`
   (a random string).
2. Add `app/externalsecret.yaml`:

   ```yaml
   ---
   # yaml-language-server: $schema=https://k8s-schemas.home-operations.com/external-secrets.io/externalsecret_v1.json
   apiVersion: external-secrets.io/v1
   kind: ExternalSecret
   metadata:
     name: konflate-webhook-token
   spec:
     secretStoreRef:
       kind: ClusterSecretStore
       name: onepassword-connect
     target:
       name: konflate-webhook-token-secret
       template:
         data:
           KONFLATE_WEBHOOK_SECRET: "{{ .KONFLATE_WEBHOOK_SECRET }}"
     dataFrom:
       - extract:
           key: konflate
   ```

3. Add `./externalsecret.yaml` to `app/kustomization.yaml` and set in the
   HelmRelease values:

   ```yaml
   secret:
     existingSecret: konflate-webhook-token-secret
   ```

4. In GitHub repository settings → Webhooks, POST `https://konflate.${SECRET_DOMAIN}/hooks`
   (content-type `application/json`, secret = the same value, events =
   Pull requests + Pushes).
