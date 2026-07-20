# Talos Cluster

> A single-tenant **home Kubernetes cluster** built on Talos Linux and managed entirely by GitOps
> (Flux). This site is the knowledge base for how it works, how to operate it, and the gotchas worth
> remembering — for the maintainer and for AI agents working in the repo.

```mermaid
flowchart LR
    A[Git push] --> B[Flux detects change]
    B --> C[Reconcile Kustomizations]
    C --> D[Deploy HelmReleases]
    D --> E[Workloads running]
    style A fill:#0066cc,color:#fff,stroke:#003366,stroke-width:2px
```

Everything that runs in the cluster is declared in this public repository. Secrets never live in Git:
they flow from 1Password through External Secrets into Kubernetes `Secret`s, and Flux substitutes
`${SECRET_DOMAIN}` / `${SECRET_INTERNAL_DOMAIN}` placeholders at apply time. Because the repo is
public, internal addresses, node and device hostnames, and MACs are kept out of it — a CI guard
enforces that on every pull request.

## Navigate the knowledge base

- **Architecture** — how the pieces fit: the GitOps flow, networking (Cilium + Envoy Gateway),
  storage, secret management, and the AI/LLM stack.
- **Operations** — runbooks: bootstrapping, Talos and Kubernetes upgrades, backups, and monitoring
  the cluster and the infrastructure around it.
- **Migrations** — playbooks for the larger changes the cluster has been through.
- **Apps** — per-application notes: why it is set up the way it is, and the traps hit while deploying
  it.
- **Troubleshooting** — a symptom ladder and a KB of recurring issues with their fixes.
- **FAQ** — quick answers to the questions that come up most.

## Tech at a glance

| Area | Choice |
| ---- | ------ |
| OS | Talos Linux (immutable) |
| GitOps | Flux |
| CNI | Cilium |
| Ingress | Envoy Gateway (Gateway API) |
| Secrets | External Secrets + 1Password |
| Storage | Rook-Ceph + miroir |
| Backups | VolSync (Kopia) |
