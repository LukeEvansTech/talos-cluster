# Device & Infrastructure Monitoring

How the cluster scrapes external infrastructure (storage, hypervisor, firewall,
switches) into Prometheus, and — the important part — **how to change the
settings later** without hunting through manifests.

For the TrueNAS-specific exporter install steps see
[`truenas-monitoring.md`](./truenas-monitoring.md); this document is the
cross-cutting "what lives where and how to update it" reference.

## What is monitored

| Target         | Exporter                          | App (`kubernetes/apps/observability/`)        | Address source                                     | Credentials (1Password, vault `Talos`) |
| -------------- | --------------------------------- | --------------------------------------------- | -------------------------------------------------- | -------------------------------------- |
| TrueNAS host   | node-exporter / smartctl-exporter | `kube-prometheus-stack` (`scrapeconfig.yaml`) | `${SECRET_STORAGE_SERVER}`                         | — (Docker apps on TrueNAS)             |
| TrueNAS ZFS    | graphite bridge                   | `truenas-exporter`                            | `${SECRET_STORAGE_SERVER}`                         | — (Custom App on TrueNAS)              |
| vCenter / ESXi | `pryorda/vmware_exporter`         | `vmware-exporter`                             | `${SECRET_VSPHERE_ENDPOINT}`                       | `vsphere-monitoring`                   |
| OPNsense       | `AthennaMind/opnsense-exporter`   | `opnsense-exporter`                           | `host` field in item                               | `opnsense-exporter`                    |
| MikroTik ×2    | `akpw/mktxp`                      | `mktxp`                                       | `${MIKROTIK_POE_ADDR}` / `${MIKROTIK_NONPOE_ADDR}` | `mktxp`                                |
| Mellanox Onyx  | `prometheus/snmp_exporter`        | `snmp-exporter`                               | `${ONYX_ADDR}`                                     | — (SNMP community `cr-onyx-ro`)        |

All of these run on **least-privilege, dedicated read-only accounts**, never an
admin credential.

## Where the settings live

There are four places a setting can live. Knowing which one a value uses is the
whole game:

1. **DNS names** — switch/host addresses are DNS records, not raw IP addresses.
   Host-overrides are declared in the `network-ops` repository
   (`ansible/vars/dns.yml`, applied by `ansible/playbooks/opnsense-dns.yml`) and
   served by OPNsense Unbound.
    - `sw-main-core.core.codelooks.com` → Onyx
    - `sw-comms-access.core.codelooks.com` → MikroTik PoE
    - `sw-main-mgmt.core.codelooks.com` → MikroTik management
2. **`cluster-settings`** (non-secret, Git-tracked) —
   `kubernetes/components/global-vars/cluster-settings.yaml`. Holds the DNS names
   the monitoring manifests reference as `${...}`: `ONYX_ADDR`,
   `MIKROTIK_POE_ADDR`, `MIKROTIK_NONPOE_ADDR`.
3. **`cluster-secrets`** (1Password-backed) — `SECRET_STORAGE_SERVER`,
   `SECRET_VSPHERE_ENDPOINT`. The Git copy under `components/global-vars/` is a
   placeholder; the real values come from the `cluster-secrets` 1Password item.
4. **Per-app 1Password items** (vault `Talos`, read by External Secrets via the
   `onepassword-connect` ClusterSecretStore) — the device credentials themselves.

> Device **admin** credentials (used by `network-ops` to manage the devices)
> live in the **`Home Operations`** vault (`Network-OPNsense`,
> `Network-MikroTik-PoE`/`-NonPoE`, `Network-Onyx`). The cluster never reads
> these; it only reads the dedicated read-only items in `Talos`.

## Maintenance recipes

### Renumber a device (its IP changed)

Because monitoring references **DNS names**, this is a one-line change:

1. Edit the `server:` for that host in `network-ops` `ansible/vars/dns.yml`.
2. Apply: `op run --env-file=.env -- ansible-playbook ansible/playbooks/opnsense-dns.yml`
   (or `mise run opnsense-dns`). Requires the `ansibleguy.opnsense` collection
   and `httpx` in the ansible Python — see Gotchas.
3. Nothing in this repository changes; the exporters re-resolve the name on the
   next scrape.

For TrueNAS / vCenter the address is a `cluster-secrets` variable instead — edit
`SECRET_STORAGE_SERVER` / `SECRET_VSPHERE_ENDPOINT` in the `cluster-secrets`
1Password item; ExternalSecrets pick it up on the next refresh.

### Change the DNS name a monitor uses

Edit the relevant variable in `cluster-settings.yaml` (e.g. `ONYX_ADDR`) and open
a pull request. Flux substitutes it into the manifests on reconcile. For
`snmp-exporter` you must then restart the pod (see Gotchas).

### Rotate a credential

1. Update the value in the per-app 1Password item (vault `Talos`).
2. Force ExternalSecrets to re-pull immediately (otherwise it waits up to the
   refresh interval):
   `kubectl annotate externalsecret <name> -n observability force-sync="$(date +%s)" --overwrite`
3. The app's `reloader.stakater.com/auto` annotation restarts the pod when the
   rendered secret changes. **Exception: `snmp-exporter` has no reloader** —
   restart it manually (see Gotchas).

### Enable / disable an OPNsense collector

The exporter exposes a flag per collector. To silence a broken or unwanted one,
add an env var to `opnsense-exporter/app/helmrelease.yaml`, e.g.
`OPNSENSE_EXPORTER_DISABLE_UNBOUND: "true"`. Run
`/opnsense-exporter --help` in the pod to list `--exporter.disable-*` flags.

### Add a new SNMP device

Add an entry under `serviceMonitor.params` in
`snmp-exporter/app/helmrelease.yaml` (chart `9.14.0` reads targets there, **not**
a top-level `params`):

```yaml
- name: <short-name>
  target: ${SOME_ADDR} # a cluster-settings DNS var
  module: [if_mib] # or [if_mib, entity_sensor] for sensors
  auth: [public_v2] # or a custom auth defined in configmap-entity-sensor
  interval: 60s
  scrapeTimeout: 30s
  relabelings:
      - sourceLabels: [__param_target]
        targetLabel: instance
```

A custom SNMP module or auth goes in `configmap-entity-sensor.yaml`; it is merged
with the image's bundled `snmp.yml` via the two `--config.file` `extraArgs`.

### Add a new RouterOS switch to mktxp

1. Create a read-only user on the switch:
   `/user group add name=prometheus policy=api,read` then
   `/user add name=mktxp group=prometheus password=<from 1Password>`, and enable
   `api-ssl` restricted to the node source IPs.
2. Add the host as a `cluster-settings` DNS var and append a `[Section]` to the
   `mktxp.conf` block in `mktxp/app/externalsecret.yaml`.

## Validation

Check every device collector at once:

```bash
kubectl exec -n observability pod/prometheus-kube-prometheus-stack-0 -c prometheus -- \
  promtool query instant http://localhost:9090 \
  'up{job=~"truenas-graphite-exporter|vmware-exporter|opnsense-exporter|mktxp|snmp-exporter"}'
```

Or per job, with a metric-name count to confirm real data is flowing:

```bash
promtool query instant http://localhost:9090 'count(group by (__name__)({job="mktxp"}))'
```

Direct-scrape an SNMP target through the exporter (bypasses Prometheus):

```bash
kubectl exec -n observability deploy/snmp-exporter -- \
  wget -qO- 'http://localhost:9116/snmp?target=sw-main-core.core.codelooks.com&module=if_mib,entity_sensor&auth=onyx_ro'
```

## Gotchas

- **`snmp-exporter` has no reloader.** A ConfigMap, module, or auth change does
  **not** restart the pod, so the new config is not loaded. After any change run
  `kubectl rollout restart deploy/snmp-exporter -n observability`.
- **Never write a literal `${...}` in a YAML comment** in a manifest. Flux's
  post-build `envsubst` parses it as a variable name and fails the whole
  Kustomization with `unable to parse variable name`.
- **`mktxp` and `pryorda/vmware_exporter` use binary/API protocols, not REST.**
  A `401` when you test the RouterOS REST API is expected — the read-only user
  intentionally lacks the `web` policy; mktxp uses the binary API on `8729`.
- **The `network-ops` ansible needs one-time setup** the repository does not
  automate: `ansible-galaxy collection install -r ansible/requirements.yml`,
  `httpx` available to the ansible Python (the `ansibleguy.opnsense` module needs
  it), and `OPNSENSE_API_KEY` / `OPNSENSE_API_SECRET` present in the local `.env`.
- **TrueNAS ZFS metrics** require the graphite Custom App to be installed on
  TrueNAS itself (see `truenas-monitoring.md`); until then
  `truenas-graphite-exporter` shows `up=0`.
