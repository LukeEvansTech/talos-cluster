# Cloudflare DNS

## Purpose

`cloudflare-dns` runs [external-dns](https://github.com/kubernetes-sigs/external-dns) with the
Cloudflare provider, publishing public records for the `envoy-external` gateway's HTTPRoutes and
any `DNSEndpoint` it watches. It runs `policy: sync`, so it deletes any record it no longer sees
as required. `opnsense-dns` does the equivalent job for the internal zone against OPNsense; the
two run independent configs.

## Design decisions

- `annotationPrefix` is pinned to the alpha form (`external-dns.alpha.kubernetes.io/`) instead of
  the chart default. external-dns 1.22.0 changed that default to the GA form,
  `external-dns.kubernetes.io/`, and every external-dns annotation in this repository still uses
  the alpha form. Because this instance runs `policy: sync`, an unpinned bump would read zero
  annotations and reconcile every owned record down to nothing, deleting it. external-dns sits
  outside the "Protected infra" Renovate rule, so this pin is what keeps a routine minor-version
  automerge from taking down public DNS
  ([#5108](https://github.com/LukeEvansTech/talos-cluster/pull/5108)). `opnsense-dns` carries the
  identical pin for the same reason. Dropping either pin is only safe alongside migrating every
  annotation in the repository to the GA form, the same order upstream's own home-ops config
  followed after hitting this
  ([home-ops@a0120fc](https://github.com/onedr0p/home-ops/commit/a0120fcd05d27b69d2487ec7cbd945a2caa06f14)).
