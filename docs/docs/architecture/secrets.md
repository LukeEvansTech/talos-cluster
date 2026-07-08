# Secret management

Secrets never live in Git. They flow:

```text
1Password → ExternalSecret → Kubernetes Secret → consumed by the workload
```

## The pieces

- A `ClusterSecretStore` named `onepassword-connect` reads the `Talos` 1Password vault.
- Per-app `externalsecret.yaml` files reference specific 1Password items by title. Apps that use one
  should `dependsOn` `onepassword-connect` in `external-secrets`.
- **`cluster-secrets`** — a single 1Password item extracted into a Secret and injected into every
  app's `postBuild.substituteFrom`. Holds cluster-wide *sensitive* values, including
  `${SECRET_DOMAIN}`, `${SECRET_INTERNAL_DOMAIN}`, and internal device DNS names.
- **`cluster-settings`** — a git-tracked ConfigMap (`components/global-vars/`) holding cluster-wide
  *non-sensitive* `${...}` values (non-secret feature flags and the like; currently empty).

## Rules for a public repo

- Use the `${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}` placeholders in Git; Flux substitutes the
  real values at apply time.
- Any literal `${VAR}` you want to survive substitution (Grafana dashboards, envsubst templates,
  shell snippets) must be escaped as `$${VAR}`.
- **Device address / lookup tables** (e.g. router backup inventories, SNMP/NUT targets) must be
  templated inside an ExternalSecret's `target.template.data` block and mounted from the rendered
  Secret — never rendered into a ConfigMap in Git. Internal device DNS names live in the
  `cluster-secrets` 1Password item, not in `cluster-settings`.
- A CI guard (`.github/scripts/check_internal_identifiers.py`, run by the security-scans workflow)
  fails any pull request that introduces a LAN IP, node name, site-prefixed device hostname
  (`cr-*` / `sw-*`), MAC address, or internal hostname (`.lan` / `.internal`) outside a small
  allowlist of accepted functional configs. Device models are intentionally not enumerated in the
  script to avoid self-disclosure in this public repository.

## Talos machine secrets (talsecret)

The talhelper secrets bundle (cluster CA/PKI, etcd certs, bootstrap tokens) follows the same
"1Password owns it" rule as everything else. There is **no SOPS anywhere in this repository** —
the historical `talos/talsecret.sops.yaml` was removed in PR #3463 (2026-07) after the age key for
it was lost; the encrypted blob left in git history is dead ciphertext.

- The bundle is stored as the **`talsecret` document** in the `Talos` 1Password vault.
- `just talos gen-config` fetches it (`op document get talsecret --vault Talos`) to a temp file,
  runs `talhelper genconfig`, and cleans up. Node configs land in the gitignored
  `talos/clusterconfig/`.
- `just talos gen-secret` creates the 1Password document (from `talhelper gensecret`) only if it
  does not already exist.
- **Recovery without 1Password:** any previously generated node config contains the full secret
  material, so the bundle can be reconstructed offline from the gitignored output of a prior run:

  ```bash
  talosctl gen secrets \
    --from-controlplane-config talos/clusterconfig/kubernetes-<node>.yaml \
    -o /tmp/talsecret.yaml --force
  ```

  This was how the bundle was recovered when the age key disappeared — regenerated configs were
  verified byte-identical before the SOPS file was deleted.
