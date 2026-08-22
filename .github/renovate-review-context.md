<!--
Repo context for the Renovate reviewer.

Spliced into the review prompt by the shared-workflows renovate-review reusable,
in place of the generic "## Repo Context" section. Everything else in the prompt
is generic and lives in the reusable — keep only repo-specific facts here.

Read from the PR's BASE commit, not the PR head, so a pull request cannot
rewrite the rules it is about to be judged by. Edits therefore only take effect
once merged to the default branch.

Starts at H2 deliberately: this is a fragment spliced into a larger prompt,
not a standalone document, so MD041's top-level-heading rule does not apply.
-->
<!-- markdownlint-disable-file MD041 -->

## Repository Context

This is a Flux GitOps repository managing a single homelab Kubernetes cluster running
Talos Linux (3 control-plane nodes, no separate workers). Dependencies are primarily:

- Container images referenced in Kubernetes manifests, pinned with SHA256 digests
- Helm chart versions in HelmRelease CRDs (sourced from OCIRepository, not HelmRepository)
- Custom dependencies managed via regular expression in YAML files (see the `customManagers` block in
  .renovaterc.json5 — the former .renovate/customManagers.json5 was inlined into it)

Architecture details relevant to impact assessment:

- **App pattern**: most apps use the bjw-s/app-template chart via OCIRepository + a
  HelmRelease referencing it. PostBuild variable substitution (${SECRET_DOMAIN},
  ${TIMEZONE}, etc.) comes from a `cluster-secrets` Secret, injected by the root Flux
  Kustomization patch in kubernetes/flux/cluster/ks.yaml.
- **CNI / network policy**: Cilium (also provides Gateway API).
- **Ingress / routing**: Envoy Gateway with HTTPRoute only (NOT Traefik/Ingress).
  Parent refs point to envoy-internal / envoy-external gateways in the network namespace.
- **Storage**: Rook-Ceph block (default StorageClass), NFS media mounts, VolSync backups.
- **Secrets**: ExternalSecret CRDs backed by 1Password via a ClusterSecretStore
  (no plaintext secrets in-repo).

High-blast-radius components that warrant deeper scrutiny: the "Protected infra" entry
in `.renovaterc.json5` is the single source of truth for which components are
cluster-critical — networking, storage, secrets/certs, the GitOps engine, and the node OS
itself. This workflow derives its blast-radius regular expression from that same entry at runtime, so
the two can never disagree.

When assessing impact (step 3), the files in this repository that reference or consume a dependency
are: HelmRelease CRDs, Kustomizations, ConfigMaps, ExternalSecrets, HTTPRoutes, and anything
else that touches the dependency.
