# Shared components

## Purpose

`kubernetes/components` holds the reusable Kustomize components every app draws on: `homepage`
(dashboard tiles), `global-vars` (`cluster-secrets` and `cluster-settings`, substituted into every
app), `alerts`, `volsync` (NFS and remote backup wiring), `kopiur` (snapshot-based backup wiring),
and `zeroscaler`.

## Homepage: route-based discovery

Homepage discovers services from annotated HTTPRoutes, not Service annotations, which is where
this component used to put them. `app-template` renders the route under `spec.values.route.app`,
so every consumer must define its route there. `gethomepage.dev/app` pins the pod selector to the
release name; some HTTPRoutes are named `<app>-app`, which would otherwise match no pods.

## cluster-settings: internal hostnames stay out of Git

Network device addresses (`ONYX_ADDR`, `MIKROTIK_POE_ADDR`, `MIKROTIK_NONPOE_ADDR`) live in the
`cluster-secrets` 1Password item, not in the Git-tracked `cluster-settings` ConfigMap, because this
is a public repository and those are internal hostnames. Flux still substitutes `${...}` from
`cluster-secrets`, so consumers are unaffected. To change one, edit the field in the
`cluster-secrets` item (`Talos` vault) and force-sync the ExternalSecret.

## cluster-secrets: the create-once placeholder Secret

`kubernetes/components/global-vars/cluster-secrets.yaml` is a placeholder `Secret` so CI (flate)
can render `${VAR}` substitutions; it is load-bearing because charts with a `values.schema.json` or
a load-balancer IP field reject empty values. The `ExternalSecret` overwrites it with real values
in-cluster.

Its `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent` annotation stops kustomize-controller
re-applying these placeholders over the ExternalSecret-managed real ones on every reconcile; see
[KB-020](../troubleshooting/kb/020-httproute-drifts-to-placeholder-hostnames.md) for the incident
this fixed. The trade-off: a key added to this file after bootstrap reaches only CI rendering, not
the live Secret, which gets new keys solely through the 1Password item, so update the item first.
The fail-loud placeholders below still apply on a fresh bootstrap, which is when they matter.

### Fail-loud placeholders

A subset of the placeholder values are deliberately unroutable addresses rather than ordinary fake
values, so that if the ExternalSecret ever fails to overwrite this Secret, the consumer fails
loudly (hits a dead address) instead of silently succeeding against the wrong host:

- `SEEDBOX_TAILNET_ADDR`: the top of the CGNAT range; the seedbox tailnet scrape target.
- `SEEDBOX_PUBLIC_IP`: TEST-NET-1 (RFC 5737); the seedbox's public address, probed by blackbox
  independent of the tailnet scrape path.
- `NUT_SERVER_ADDR`: TEST-NET-1; the estate's NUT server, an appliance outside the cluster,
  consumed by `nut-exporter`'s ServiceMonitor. See
  [device monitoring](../operations/device-monitoring.md) for why it runs outside the cluster.
- `OPNSENSE_ADDR`: TEST-NET-1; the firewall's LAN address, scraped by the `opnsense-node`
  ScrapeConfig for OS-level metrics the API exporter cannot see.
- `PRINTER_HL_ADDR`, `PRINTER_MFC_ADDR`, `PRINTER_QL_ADDR`, `PRINTER_PT_ADDR`: TEST-NET-1; the
  Brother printer fleet, polled by `snmp-exporter` over SNMPv3. Real addresses live in the
  `cluster-secrets` 1Password item.

## volsync: shared kopia cache size (NFS path only)

`cacheCapacity: 16Gi` in the `volsync` component's `nfs` ReplicationSource template is a shared
size, not a per-app one: the cache holds the shared kopia repository's index set, so every mover
converges on the same footprint regardless of the app's own data size. A tiny app peaks within
0.1Gi of a large one.

That footprint grows about 25MiB a day with the repository (7.23Gi on 2026-08-29, 7.57Gi on
2026-09-12), so a size chosen against today's number expires: the old 8Gi per-app override went
critical on 2026-09-11, and 10Gi was about 29 days from warning. 16Gi is about 8 months of headroom
at the measured rate. `miroir-local` is thin-provisioned (1.1TiB allocated against 2.1TiB
provisioned) and real usage stays at the shared ~7.5Gi, so declaring more capacity costs almost
nothing on disk. Raising this value only buys time; the lever on the growth itself is kopia
retention, not cache size or more frequent maintenance.

This does not apply to the `remote` templates. Those use `spec.restic` with a distinct
`RESTIC_REPOSITORY` per app (`kubernetes/components/volsync/remote/externalsecret.yaml` templates
it with a `/${APP}` suffix), so there is no shared repository index and this growth math does not
transfer. Their `cacheCapacity: 16Gi` is the same literal default, unverified against any
per-app-repository growth measurement.
