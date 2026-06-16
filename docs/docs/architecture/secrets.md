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
  *non-sensitive* values (e.g. the default Ollama model).

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
  fails any pull request that introduces a LAN IP, node or device hostname, MAC, internal hostname,
  or device model outside a small allowlist of accepted functional configs.
