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
| UPS (NUT)      | `hon95/prometheus-nut-exporter`   | `nut-exporter`                                | `${NUT_SERVER_ADDR}`         | — (anonymous NUT protocol read)        |
| Server BMCs    | `mrlhansen/idrac_exporter`        | `bmc-exporter`                                | ExternalSecret (`/discover`) | one item per BMC (shared with certwarden) |
| Workstation BMC | `mrlhansen/idrac_exporter`       | `bmc-exporter-workstation`                    | ExternalSecret (`/discover`) | `workstation-ipmi`                     |

All of these run on **least-privilege, dedicated read-only accounts**, never an
admin credential.

Since PR #3768 (2026-07-21), UPS monitoring follows the same shape as everything
else in this table: `nut-exporter` scrapes an **external** NUT appliance (a
dedicated box outside the cluster, so it can keep reporting and sequence the
cluster's own shutdown through a power event the cluster itself doesn't
survive) at `${NUT_SERVER_ADDR}`. There is no in-cluster `upsd` anymore; reading
NUT variables is anonymous, so no credential is involved.

The `peanut` web UI moved onto that same appliance in PR #3775 and is no longer
deployed here. It was the last piece of UPS monitoring still hosted on the
cluster — which a power event is precisely what shuts down, so the dashboard
went dark at the one moment it was wanted. `nut-exporter` stays: its value is
history in Prometheus, which is a cluster concern either way.

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

### Add a BMC to Redfish monitoring

`bmc-exporter` is a multi-target Redfish exporter for the Supermicro BMC fleet,
added by PR #4022. Everything about a BMC — hostname, username, password — lives
in `app/externalsecret.yaml`, which templates the exporter's whole `idrac.yml`
and mounts it via the chart's `existingSecret`. There is no second target list:
Prometheus discovers targets from the exporter's own `/discover` endpoint, so
the ExternalSecret is the single source of truth.

To add one:

1. Create (or reuse) the per-BMC item in the `Talos` vault with `IPMI_USERNAME`
   and `IPMI_PASSWORD`. The fleet already has one item per BMC because
   certwarden uses the same items to deploy certificates to these boards.
2. Add a `data:` pair pointing at that item, and a matching entry under `hosts:`
   in the templated `idrac.yml`.
3. Add the same host to the `lan-icmp` Probe in
   `blackbox-exporter/lan/probes.yaml` if it is not already there — see below
   for why both exist.

Credentials are **not** uniform across the fleet: it shares one username but
every board has its own password, so `hosts.default` cannot be used and each BMC
needs its own entry.

**A BMC with no DNS record** is keyed by an address held in its 1Password item
(`IPMI_ADDR`) and templated into the rendered Secret, so the address never
reaches git. Two hosts use this today. Prefer a DNS name where one exists — the
address form exists because `network-ops` owns internal DNS and adding a record
there is a separate change against the firewall. If a record is created later,
switch the host to its name and drop `IPMI_ADDR`.

### Finding BMCs that are not yet monitored

Sweep the management VLAN for the IPMI port rather than trusting any inventory:

```bash
nmap -Pn -p 623,443 --open -oG - <mgmt-vlan>/24 | awk '/623\/open/ {print $2}'
```

Then check each hit for Redfish with `curl -sk https://<addr>/redfish/v1/` — the
unauthenticated root returns the vendor and Redfish version, which is enough to
tell a real Redfish BMC from something else listening on 623. Recorded addresses
drift: one BMC's 1Password URL pointed at an address that was firewalled off,
while the board itself answered elsewhere on the VLAN.

### Why the workstation has its own exporter deployment

`bmc-exporter-workstation` is a second single-target instance of the same chart,
and it exists purely to give that BMC a distinct `job` label. The workstation is
a desk machine that is powered off most of the time, so it must be exempt from
`BmcHostPoweredOff` while staying covered by every other rule — and excluding one
host from one rule needs something in the PromQL to match on. Nothing else works
here: `instance` is the raw address and would leak into this public repository,
the `model` label does not discriminate (that board and the storage node both
report the generic `Super Server`), and synthesising a label via relabeling would
have to match the address too.

So the rules use `job=~"bmc-exporter.*"` throughout, and `BmcHostPoweredOff`
alone pins `job="bmc-exporter"` exactly. If you add a rule, use the wide matcher
unless you specifically mean to exclude the workstation.

### Why ICMP probes and Redfish both cover the BMCs

They answer different questions and the overlap is deliberate. A Redfish scrape
needs reachability, TLS and a valid login all at once, so a failure is
ambiguous. The ICMP result is what separates a dead BMC from a broken
credential: down in both means network or BMC, up in ICMP but down in Redfish
means credentials, TLS or firmware.

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
- **A Redfish scrape is slow, and the exporter's default timeout is too short
  for these boards.** Measured on the live fleet: ~7.5s warm on the newer
  boards, ~13s on the older one, ~23s cold after a BMC restart. The exporter
  defaults to a 10s Redfish timeout, which aborts collection mid-flight and
  yields a partial scrape rather than an obvious error, so `bmc-exporter` sets
  `timeout: 45` against a 60s interval and 55s scrape timeout.
- **A powered-off host drops every sensor series.** Only `idrac_system_power_on
  0` remains — no temperatures, no fans, no PSU readings. Any alert on those
  sensors therefore cannot fire for a machine that is off, which is why
  `BmcFanStopped` is guarded on `idrac_system_power_on == 1` rather than relying
  on the series being absent.
- **Supermicro exposes no storage metrics over Redfish** on either board
  generation here — the tree exists but is not populated. Drive health stays
  with `smartctl-exporter`; do not expect `idrac_storage_*` to appear.
- **Do not read `idrac_power_supply_output_watts` as consumption.** Measured
  across the fleet, the PSU figure sums to ~7000W against ~1100W of
  `idrac_power_control_consumed_watts` — roughly 6x. It is not capacity being
  mislabelled either, since `idrac_power_supply_capacity_watts` is a separate
  series reporting the real ~2kW rating. Dashboards use the system-level metric
  for draw; the PSU figure is shown separately and labelled as reported.
- **Temperature thresholds must be split by sensor class.** CPU packages run far
  hotter than anything else here — `max by (name)` over the running fleet gives
  CPU Temp 77C against 68C for the hottest NIC and =<53C for every other rail.
  One blanket threshold either sits a few degrees off a perfectly normal CPU or
  lets a NIC cook unnoticed, so the rules band CPUs at 90/95C and everything
  else at 80/90C. Do not set these from a single sampled host: the first pass at
  this used one node reading 59C and picked 80C, which would have left 3C of
  headroom on the busiest CPUs.
- **`idrac_*_health` metrics are an enum, not a boolean**: 0=OK, 1=Warning,
  2=Critical. Alert on `> 0`, never `== 1`, or a jump straight to Critical is
  missed.
- **A `Critical` health rollup is often chassis intrusion, not a failing part.**
  Check `/redfish/v1/Chassis/1/Sensors/ChassisIntru` before hunting hardware: if
  `Reading` is 1 the switch is asserted right now, meaning the case is open or
  the switch has failed, while CPU and memory can both still report OK. It is
  also worth distinguishing from a stale latch — a SEL entry from months ago is
  history, a sensor reading 1 is current. These boards expose no intrusion-reset
  action over Redfish, so clearing it is a physical job.
- **Certwarden's cert-deploy Secrets are adopted by their Job after creation,
  not at creation.** They hold the certificate private key, and the ownerReference
  cannot be set up front because the owning Job does not exist yet. This was
  missing until 2026-07-31 and had leaked 40 Secrets since 2025-11; if orphans
  reappear, check that the `patch` verb on secrets is still in the certwarden
  Role, since the adoption silently warns rather than failing the deployment.
