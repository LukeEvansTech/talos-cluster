# Scanopy

Network-documentation app in the `network` namespace. Auto-generates
L2/L3/workload/application diagrams by continuously scanning the infrastructure.

## Purpose

- Rust network-documentation tool (AGPL-3.0) split into two workloads plus a database:
  - **server**: Rust API + bundled Svelte UI, port `60072` (`ghcr.io/scanopy/scanopy/server`).
  - **daemon**: Rust scanner (SNMP / LLDP / ARP), port `60073` (`ghcr.io/scanopy/scanopy/daemon`).
  - **postgres**: upstream compose bundles its own Postgres; here it uses the shared CNPG cluster.
- Deployed via two HelmReleases (`scanopy` server, `scanopy-daemon` scanner) that share one
  `app-template` `OCIRepository`, both referencing `chartRef.name: scanopy`.
- Internal-only app: exposed on `envoy-internal` at `scanopy.${SECRET_DOMAIN}`.

## Design decisions

- **Daemon networking: privileged `hostNetwork` Deployment (single replica).** One scanner total
  for the flat LAN /24; a per-node DaemonSet was rejected because all nodes share the same /24
  and would scan it three times, and two hostNetwork pods would contend for host port `60073`
  during a rollout. `SCANOPY_NAME: scanopy-codelooks` is a fixed static identity (previously
  derived from `spec.nodeName` when the daemon was a DaemonSet). `SCANOPY_INTERFACES=enp1s0np0`
  restricts L2 scanning to the physical LAN NIC, preventing the hostNetwork daemon from
  auto-scanning Cilium pod/service CIDRs and saturating node conntrack. This only limits L2
  scanning, though: Scanopy still L3-scans remote subnets it discovers via SNMP routing tables, so
  its conntrack footprint grows as more routable subnets appear on the network.
  `SCANOPY_CONCURRENT_SCANS=5` caps simultaneous host scans. Requires
  `dnsPolicy: ClusterFirstWithHostNet`. Multus was rejected: the only existing NAD is a macvlan
  on the same primary NIC, so it adds nothing over hostNetwork without authoring VLAN-tagged NADs.
- **Database: shared CNPG `postgres18-rw.database.svc`.** Cluster standard (metabase, netbox). A
  declarative CNPG tenant (`database/cloudnative-pg/tenants/scanopy.yaml`) provisions the DB +
  role; the Flux Kustomization `dependsOn` `cloudnative-pg-tenants` in `database`.
- **DB TLS: `?sslmode=require`.** CNPG presents a valid cert-manager cert (`postgres18-tls`, real
  SANs + issuer DN), so the chain verifies. Matches NetBox. The `sslmode=disable` used by Metabase
  is a JVM-only workaround (Java's strict TLS verifier) and does not apply to Scanopy's Rust client.
  Hardening path is `verify-full` with a mounted `ca.crt` + `sslrootcert`; never drop to `disable`.
- **Auth: built-in password login.** No OIDC/SSO in v1; first run creates the admin in the UI.
- **SMTP: internal relay** at `smtp2graph.infrastructure.svc.cluster.local:25` (unauthenticated; sends via Graph as its `scanopy@` alias).
- **SNMP community via ExternalSecret**, mounted read-only at the neutral path
  `/run/secrets/snmp-community`. The community string lives only in 1Password (repository is PUBLIC); the
  credential is then configured in the UI pointing at that file.
- **Docker-socket scan source dropped.** Talos runs containerd with no Docker socket, so it is
  disabled explicitly with `SCANOPY_ENABLE_LOCAL_DOCKER_SOCKET=false`.
- **Components**: `volsync` (`VOLSYNC_CAPACITY: 5Gi`) backing the server `/data` PVC. (Gatus
  monitoring is automatic via the gatus-sidecar chart, which auto-discovers the HTTPRoute; the old
  `gatus/guarded` DNS-check component is gone.) No PodMonitor: Scanopy exposes no Prometheus metrics.

## Deploy gotchas

- **Daemon pairing is shared state.** Every daemon and the server read `scanopy-secret` (via
  `envFrom`), sharing `SCANOPY_DAEMON_API_KEY` and `SCANOPY_NETWORK_ID` so each per-node daemon
  registers against the server over `http://scanopy.network.svc.cluster.local:60072`. Daemon
  registration may require a "network" object to exist first: create the admin + network in the UI.
- **ExternalSecret key coverage.** `scanopy-secret` must carry
  `SCANOPY_DATABASE_URL`, `SCANOPY_DAEMON_API_KEY`, `SCANOPY_NETWORK_ID`; `scanopy-snmp-secret`
  must carry `community`. These are not validated by render-time CI: grep the rendered manifests
  for every `secretRef`/`secretKeyRef` and projected `items[].key` before applying.
- **Secrets must live in the `Talos` 1Password vault** (the ClusterSecretStore only reads `Talos`).
  Create the `scanopy` item via the `op` CLI, never the UI; `SCANOPY_NETWORK_ID` is a lowercase
  UUID, and `SNMP_COMMUNITY` is the one value supplied by hand.
- **`volsync` component lives in `ks.yaml` `spec.components` only**, not also in
  `app/kustomization.yaml`, or it double-applies. The PVC it creates is named `${APP}` (`scanopy`),
  referenced by the server's `persistence.data.existingClaim`.
- **hostNetwork port `60073`** is bound on every node's host IP. Confirm nothing else uses it.
- **Daemon hostPath `/var/lib/scanopy-daemon`** holds daemon config; it persists across Talos
  reboots (`/var/lib` is writable + persistent).
- **Privileged is allowed**: the cluster enforces no restricted PodSecurity and the `network`
  namespace has no PSA labels, so the privileged hostNetwork daemon admits cleanly.
- **Image digests are pinned** for both server and daemon (kept in lockstep by Renovate).
- **Server strategy is `Recreate`, not `RollingUpdate`.** The single replica mounts an RWO
  ceph-block PVC, so a surge pod scheduled on another node during a rolling update would deadlock
  on multi-attach; Recreate tears the old pod down first. If the old pod sticks in `Terminating`,
  Helm's 5-minute timeout can thrash between upgrade and rollback: recover by suspending the
  HelmRelease, force-deleting the pod, then resuming (`cleanupOnFail` and `remediation.retries`
  bound the blast radius).

## Operational notes

- Check Kustomization + both HelmReleases reach `Ready=True`:

  ```bash
  flux -n network get kustomization scanopy
  flux -n network get helmrelease scanopy scanopy-daemon
  ```

- Verify both ExternalSecrets are `SecretSynced=True` and their target Secrets exist:

  ```bash
  kubectl -n network get externalsecret scanopy scanopy-snmp
  kubectl -n network get secret scanopy-secret scanopy-snmp-secret
  ```

- Confirm DB provisioning + TLS connect from the server pod:

  ```bash
  kubectl -n database get databaserole,database scanopy
  kubectl -n network logs deploy/scanopy -c app | grep -iE 'tls|ssl|database|connect'
  ```

- Confirm the daemon pod is running and registered:

  ```bash
  kubectl -n network get pods -l app.kubernetes.io/name=scanopy -o wide
  kubectl -n network logs deploy/scanopy-daemon | grep -iE 'register|network|server'
  ```

- After deploy, in the UI: create an SNMP credential pointing at `/run/secrets/snmp-community` and
  confirm a switch/AP/router is discovered; trigger a test email (e.g. a user invite) and confirm it
  relays via the internal SMTP relay.
- The server container runs with no restrictive `securityContext` (writes `/data`, reads
  `/app/static`); harden to `runAsNonRoot`/`readOnlyRootFilesystem` only after confirming it stays up.
- **Watch daemon conntrack usage as the network grows.** The daemon's L3 SNMP-discovered scanning
  is not limited by `SCANOPY_INTERFACES` (see Design decisions), so node conntrack usage grows as
  more subnets become routable. The validated peak was about 9% of the default `nf_conntrack_max`
  (262144) before that growth. If usage nears the limit, raise `net.netfilter.nf_conntrack_max` via
  `talos/talconfig.yaml`'s `machine.sysctls`, or narrow the scan scope per-discovery in the Scanopy
  UI.
- Follow-ups out of scope for v1: OIDC/SSO and Prometheus metrics.
