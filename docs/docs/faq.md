# FAQ

Common questions a maintainer or agent asks when working in this repository. For the longer-form
design notes, follow the links into [Architecture](architecture/overview.md).

## Where do secrets live and how do I add one?

Secrets never live in Git. They flow **1Password → ExternalSecret → Kubernetes Secret → workload**.
See [Secret management](architecture/secrets.md) for the full picture. To add one:

- For a **per-app secret**, add an `externalsecret.yaml` to the app's `app/` directory:
  - Use `secretStoreRef.kind: ClusterSecretStore`, `name: onepassword-connect` (reads the `Talos`
    1Password vault).
  - Reference the 1Password item by title, and have the app's `ks.yaml` `dependsOn`
    `onepassword-connect` in `external-secrets`.
- For a **cluster-wide value**, decide which store it belongs in:
  - `cluster-secrets` — a 1Password item injected into every app's `postBuild.substituteFrom`. Holds
    sensitive cluster-wide values such as `${SECRET_DOMAIN}`, `${SECRET_INTERNAL_DOMAIN}`, and
    internal device DNS names.
  - `cluster-settings` — a git-tracked ConfigMap in `components/global-vars/` for non-sensitive
    cluster-wide values (e.g. the default model name). Anything in here is world-visible.

## How does Flux variable substitution work, and why must I escape `$${VAR}`?

The root Kustomization patches every child with `postBuild.substituteFrom`, so Flux replaces `${VAR}`
tokens against `cluster-secrets`/`cluster-settings` at apply time.

- **Undefined variables become the empty string** — a typo silently blanks the value rather than
  erroring.
- Any literal `${VAR}` you want to survive substitution (Grafana dashboard template variables,
  envsubst templates, shell snippets in ConfigMaps) must be escaped as `$${VAR}` so Flux passes
  through a literal `${VAR}`.

## What is the difference between `${SECRET_DOMAIN}` and `${SECRET_INTERNAL_DOMAIN}`?

Both are substituted from `cluster-secrets`:

- `${SECRET_DOMAIN}` — the external domain, used for apps reachable through the Cloudflare tunnel.
- `${SECRET_INTERNAL_DOMAIN}` — the internal domain, used for LAN-only apps on the internal listener.

"Available under `${SECRET_DOMAIN}`" for a home app usually means internal DNS on the internal
listener, not public exposure. See [Networking](architecture/networking.md) and
[Split DNS](architecture/split-dns.md).

## How do I force a reconcile?

Push your change, then walk the dependency chain top-down:

```bash
# 1. Reconcile the Git source
flux reconcile source git flux-system

# 2. Reconcile the dependency chain (if a Kustomization shows "not ready")
flux reconcile kustomization external-secrets -n external-secrets
flux reconcile kustomization onepassword -n external-secrets

# 3. Reconcile the target app Kustomization
flux reconcile kustomization <app-name> -n <namespace>

# 4. Reconcile the HelmRelease
flux reconcile helmrelease <app-name> -n <namespace>
```

Many app Kustomizations `dependsOn` `external-secrets/onepassword`, so reconcile that chain first if
apps report "dependency not ready". A ConfigMap content change does not roll pods, so follow up with
`kubectl rollout restart deploy <app> -n <ns>`.

## Why is the repo public, and what must never be committed?

The repository is publicly readable, so anything committed is world-visible. **Never commit**:

- LAN IPs and internal CIDRs.
- Node and device hostnames, switch names, and MACs.
- `.lan` / `.internal` hostnames and deployment topology.
- Disk serials and vendor-specific device models that map the home network.

Use the `${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}` placeholders and let Flux substitute the
real values at apply time. A CI guard (`check-internal-identifiers.py`, run by the security-scans
workflow) fails any pull request that introduces one of these identifiers outside a small allowlist.
See [Secret management](architecture/secrets.md).

## Where do device addresses for monitoring live?

Not in ConfigMaps. Address and lookup tables (router backup inventories, SNMP/NUT targets, exporter
scrape lists) must be templated inside an ExternalSecret's `target.template.data` block and mounted
from the rendered Secret:

- Internal device DNS names and addresses live in the `cluster-secrets` 1Password item, not in
  `cluster-settings`.
- Do not render the table at boot via init + envsubst against a ConfigMap — that puts the template in
  Git. Template the file content inside the ExternalSecret and mount the rendered key directly.
