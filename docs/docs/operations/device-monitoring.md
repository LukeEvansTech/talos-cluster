# Device & Infrastructure Monitoring

How the cluster scrapes external infrastructure (storage, hypervisor, firewall,
switches) into Prometheus, and (the important part) **how to change the
settings later** without hunting through manifests.

For the TrueNAS-specific exporter install steps see
[`truenas-monitoring.md`](./truenas-monitoring.md); this document is the
cross-cutting "what lives where and how to update it" reference.

## What is monitored

| Target         | Exporter                          | App (`kubernetes/apps/observability/`)        | Address source               | Credentials (1Password, vault `Talos`) |
| -------------- | --------------------------------- | --------------------------------------------- | ---------------------------- | -------------------------------------- |
| TrueNAS host   | node-exporter / smartctl-exporter | `kube-prometheus-stack` (`scrapeconfig.yaml`) | `${SECRET_STORAGE_SERVER}`   | — (Docker apps on TrueNAS)             |
| TrueNAS ZFS    | graphite bridge                   | `truenas-exporter`                            | `${SECRET_STORAGE_SERVER}`   | — (Custom App on TrueNAS)              |
| TrueNAS Docker | docker_state_exporter (port 9419) | `truenas-exporter` (`scrapeconfig.yaml`)      | `${SECRET_STORAGE_SERVER}`   | — (Docker app on TrueNAS)              |
| vCenter / ESXi | `pryorda/vmware_exporter`         | `vmware-exporter`                             | `${SECRET_VSPHERE_ENDPOINT}` | `vsphere-monitoring`                   |
| Firewall       | `AthennaMind/opnsense-exporter`   | `opnsense-exporter`                           | `host` field in item         | `opnsense-exporter`                    |
| Core switch    | `prometheus/snmp_exporter`        | `snmp-exporter`                               | `${ONYX_ADDR}`               | — (SNMP community `<community>`)       |
| UPS (NUT)      | `hon95/prometheus-nut-exporter`   | `nut-exporter` (+ `peanut` web UI)            | `${NUT_SERVER_ADDR}`         | — (anonymous NUT protocol read)        |

All of these run on **least-privilege, dedicated read-only accounts**, never an
admin credential.

Since PR #3768 (2026-07-21), UPS monitoring follows the same shape as everything
else in this table: `nut-exporter` scrapes an **external** NUT appliance (a
dedicated box outside the cluster, so it can keep reporting and sequence the
cluster's own shutdown through a power event the cluster itself doesn't
survive) at `${NUT_SERVER_ADDR}`, and `peanut` gives it a web UI. There is no
in-cluster `upsd` anymore; reading NUT variables is anonymous, so no credential
is involved.

!!! warning "The two MikroTik CRS354 switches are currently NOT monitored"

    They were covered by the `mktxp` exporter, which was **retired on
    2026-07-19** (archived to `.archive/kubernetes/apps/observability/mktxp`).

    mktxp speaks the RouterOS **binary API on 8729**, and `api-ssl` is declared
    `disabled = true` by the network-ops Terraform hardening baseline
    (`terraform/mikrotik/hardening.tf`, since 2026-04-02). The exporter only ever
    worked because api-ssl had been enabled by hand in June and never codified; a
    `terraform apply` on 2026-07-06 reconciled that drift and the exporter went
    blind. Re-enabling it by hand is not durable: the next apply reverts it.

    Restoring coverage without touching the hardening posture means **SNMP**:
    add the two switches as `serviceMonitor.params[]` entries on `snmp-exporter`
    (module `if_mib`). SNMP is already enabled and ACL-scoped on both switches by
    the same Terraform. See network-ops issue #112.

## Where the settings live

There are four places a setting can live. Knowing which one a value uses is the
whole game:

1. **DNS names**: switch/host addresses are DNS records, not raw IP addresses.
   Host-overrides are declared in the `network-ops` repository
   (`ansible/vars/dns.yml`, applied by `ansible/playbooks/opnsense-dns.yml`) and
   served by OPNsense Unbound.
   - `<core-switch>.${SECRET_INTERNAL_DOMAIN}` → the core switch
   - `<access-switch>.${SECRET_INTERNAL_DOMAIN}` → the PoE access switch
   - `<mgmt-switch>.${SECRET_INTERNAL_DOMAIN}` → the management switch
2. **`cluster-settings`** (non-secret, Git-tracked):
   `kubernetes/components/global-vars/cluster-settings.yaml`. Holds non-sensitive
   `${...}` values the manifests reference (e.g. `OLLAMA_MODEL`).
3. **`cluster-secrets`** (1Password-backed): `SECRET_STORAGE_SERVER`,
   `SECRET_VSPHERE_ENDPOINT`, and the device DNS names `ONYX_ADDR`,
   `MIKROTIK_POE_ADDR`, `MIKROTIK_NONPOE_ADDR`, `NUT_SERVER_ADDR` (internal
   hostnames kept out of this public repo). Flux substitutes `${...}` from this
   Secret the same way; the real values live in the `cluster-secrets` 1Password
   item (vault `Talos`).
4. **Per-app 1Password items** (vault `Talos`, read by External Secrets via the
   `onepassword-connect` ClusterSecretStore): the device credentials themselves.

> Device **admin** credentials (used by `network-ops` to manage the devices)
> live in the **`Home Operations`** vault (separate per-device admin items). The
> cluster never reads these; it only reads the dedicated read-only items in `Talos`.

## Maintenance recipes

### Renumber a device (its IP changed)

Because monitoring references **DNS names**, this is a one-line change:

1. Edit the `server:` for that host in `network-ops` `ansible/vars/dns.yml`.
2. Apply: `op run --env-file=.env -- ansible-playbook ansible/playbooks/opnsense-dns.yml`
   (or `mise run opnsense-dns`). Requires the `ansibleguy.opnsense` collection
   and `httpx` in the Ansible Python (see Gotchas).
3. Nothing in this repository changes; the exporters re-resolve the name on the
   next scrape.

For TrueNAS / vCenter the address is a `cluster-secrets` variable instead: edit
`SECRET_STORAGE_SERVER` / `SECRET_VSPHERE_ENDPOINT` in the `cluster-secrets`
1Password item; ExternalSecrets pick it up on the next refresh.

### Change the DNS name a monitor uses

Edit the relevant field (e.g. `ONYX_ADDR`) in the `cluster-secrets` 1Password item
(vault `Talos`). The `cluster-secrets` ExternalSecret is replicated into every
namespace; it refreshes within 1h, or force-sync the consuming namespace now, e.g.
`kubectl annotate externalsecret cluster-secrets -n observability force-sync="$(date +%s)" --overwrite`.
Flux re-substitutes it into the manifests on the next reconcile. `snmp-exporter`
carries a `reloader.stakater.com/auto: "true"` annotation (see Gotchas), so
Reloader restarts its pod automatically once the rendered config changes, so
no manual rollout is needed.

### Rotate a credential

1. Update the value in the per-app 1Password item (vault `Talos`).
2. Force ExternalSecrets to re-pull immediately (otherwise it waits up to the
   refresh interval):
   `kubectl annotate externalsecret <name> -n observability force-sync="$(date +%s)" --overwrite`
3. The app's `reloader.stakater.com/auto` annotation restarts the pod when the
   rendered secret changes. `snmp-exporter` gets this annotation via a
   `postRenderers` kustomize patch rather than a chart value (see Gotchas).

### Enable / disable an OPNsense collector

The exporter exposes a flag per collector. To silence a broken or unwanted one,
add an env var to `opnsense-exporter/app/helmrelease.yaml`, e.g.
`OPNSENSE_EXPORTER_DISABLE_UNBOUND: "true"`. Run
`/opnsense-exporter --help` in the pod to list `--exporter.disable-*` flags.

### Add a new SNMP device

Add an entry under `serviceMonitor.params` in
`snmp-exporter/app/helmrelease.yaml` (the chart's 9.x line reads targets there,
**not** a top-level `params`; see `app/ocirepository.yaml` for the current pin):

```yaml
- name: <short-name>
  target: ${SOME_ADDR} # a cluster-secrets DNS var (1Password, not git)
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

### Add a RouterOS switch to snmp-exporter

The `mktxp` route is retired (see the warning above). Add RouterOS switches the
same way as the Onyx core switch: a `serviceMonitor.params[]` entry with module
`if_mib` and the read-only SNMP community. SNMP is enabled and source-ACLed on
both MikroTiks by the network-ops Terraform (`routeros_snmp_community`), so no
device-side change is needed. Add the host as a `cluster-secrets` DNS var (the
1Password item, **not** git-tracked `cluster-settings`: device hostnames stay
out of this public repo).

## Validation

Check every device collector at once:

```bash
kubectl exec -n observability pod/prometheus-kube-prometheus-stack-0 -c prometheus -- \
  promtool query instant http://localhost:9090 \
  'up{job=~"truenas-graphite-exporter|truenas-docker-exporter|vmware-exporter|opnsense-exporter|snmp-exporter"}'
```

Or per job, with a metric-name count to confirm real data is flowing:

```bash
promtool query instant http://localhost:9090 'count(group by (__name__)({job="snmp-exporter"}))'
```

Direct-scrape an SNMP target through the exporter (bypasses Prometheus):

```bash
kubectl exec -n observability deploy/snmp-exporter -- \
  wget -qO- 'http://localhost:9116/snmp?target=<core-switch>.${SECRET_INTERNAL_DOMAIN}&module=if_mib,entity_sensor&auth=<community>'
```

## Gotchas

- **`snmp-exporter` restarts on config change via Reloader (fixed by #3572,
  2026-07-17).** It previously needed a manual `kubectl rollout restart` after
  every ConfigMap/module/auth change, because it only reads its `--config.file`
  at startup. The chart's `9.x` line has no Deployment-level annotations value,
  so the fix is a `postRenderers` kustomize patch in
  `snmp-exporter/app/helmrelease.yaml` that adds
  `reloader.stakater.com/auto: "true"` directly to the Deployment. Reloader now
  restarts the pod automatically whenever the rendered ConfigMap/Secret changes,
  so no manual restart is needed anymore.
- **Never write a literal `${...}` in a YAML comment** in a manifest. Flux's
  post-build `envsubst` parses it as a variable name and fails the whole
  Kustomization with `unable to parse variable name`.
- **A scrape returning `200` with an empty body is not a healthy scrape.** An
  exporter that loses every downstream target still answers, so `up == 1` and any
  `count(metric) < N` alert compares against an **empty vector** and can never
  fire. Pair every such alert with `absent(metric)`. This blind spot hid a total
  mktxp outage for eight days.
- **`pryorda/vmware_exporter` uses an API protocol, not REST.** Likewise, a `401`
  when testing the RouterOS REST API is expected: the read-only user
  intentionally lacks the `web` policy.
- **The `network-ops` Ansible setup needs one-time steps** the repository does not
  automate: `ansible-galaxy collection install -r ansible/requirements.yml`,
  `httpx` available to the Ansible Python (the `ansibleguy.opnsense` module needs
  it), and `OPNSENSE_API_KEY` / `OPNSENSE_API_SECRET` present in the local `.env`.
- **TrueNAS ZFS metrics** require the graphite Custom App to be installed on
  TrueNAS itself (see `truenas-monitoring.md`); until then
  `truenas-graphite-exporter` shows `up=0`.
