# Scanopy

Network-documentation app in the `network` namespace. Auto-generates
L2/L3/workload/application diagrams by continuously scanning the infrastructure.

## Purpose

- Rust network-documentation tool (AGPL-3.0) split into two workloads plus a database:
  - **server** — Rust API + bundled Svelte UI, port `60072` (`ghcr.io/scanopy/scanopy/server`).
  - **daemon** — Rust scanner (SNMP / LLDP / ARP), port `60073` (`ghcr.io/scanopy/scanopy/daemon`).
  - **postgres** — upstream compose bundles its own Postgres; here it uses the shared CNPG cluster.
- Deployed via two HelmReleases (`scanopy` server, `scanopy-daemon` scanner) that share one
  `app-template` `OCIRepository`, both referencing `chartRef.name: scanopy`.
- Internal-only app: exposed on `envoy-internal` with dual hostnames
  `scanopy.${SECRET_DOMAIN}` and `scanopy.${SECRET_INTERNAL_DOMAIN}`.

## Design decisions

- **Daemon networking — privileged `hostNetwork` DaemonSet.** One scanner per node (named per node
  via `SCANOPY_NAME` from `spec.nodeName`), giving L2 reach to the nodes' primary LAN plus
  L3/SNMP reach to anything routable on the management network. Multus was rejected: the only
  existing NAD is a macvlan on the same primary NIC, so it adds nothing over hostNetwork without
  authoring VLAN-tagged NADs. Requires `dnsPolicy: ClusterFirstWithHostNet`.
- **Database — shared CNPG `postgres18-rw.database.svc`.** Cluster standard (metabase, netbox). A
  `postgres-init` initContainer provisions the DB + user; the Flux Kustomization `dependsOn`
  `cloudnative-pg-cluster` in `database`.
- **DB TLS — `?sslmode=require`.** CNPG presents a valid cert-manager cert (`postgres18-tls`, real
  SANs + issuer DN), so the chain verifies. Matches NetBox. The `sslmode=disable` used by Metabase
  is a JVM-only workaround (Java's strict TLS verifier) and does not apply to Scanopy's Rust client.
  Hardening path is `verify-full` with a mounted `ca.crt` + `sslrootcert` — never drop to `disable`.
- **Auth — built-in password login.** No OIDC/SSO in v1; first run creates the admin in the UI.
- **SMTP — internal relay** at `smtp-relay.infrastructure.svc.cluster.local:25` (unauthenticated).
- **SNMP community via ExternalSecret**, mounted read-only at the neutral path
  `/run/secrets/snmp-community`. The community string lives only in 1Password (repo is PUBLIC); the
  credential is then configured in the UI pointing at that file.
- **Docker-socket scan source dropped.** Talos runs containerd with no Docker socket, so it is
  disabled explicitly with `SCANOPY_ENABLE_LOCAL_DOCKER_SOCKET=false`.
- **Components** — `gatus/guarded` (DNS check on `${GATUS_SUBDOMAIN}.${SECRET_DOMAIN}`, not an HTTP
  probe) and `volsync` (`VOLSYNC_CAPACITY: 5Gi`) backing the server `/data` PVC. No PodMonitor:
  Scanopy exposes no Prometheus metrics.

## Deploy gotchas

- **Daemon pairing is shared state.** Every daemon and the server read `scanopy-secret` (via
  `envFrom`), sharing `SCANOPY_DAEMON_API_KEY` and `SCANOPY_NETWORK_ID` so each per-node daemon
  registers against the server over `http://scanopy.network.svc.cluster.local:60072`. Daemon
  registration may require a "network" object to exist first — create the admin + network in the UI.
- **ExternalSecret key coverage.** `scanopy-secret` must carry `INIT_POSTGRES_*`,
  `SCANOPY_DATABASE_URL`, `SCANOPY_DAEMON_API_KEY`, `SCANOPY_NETWORK_ID`; `scanopy-snmp-secret`
  must carry `community`. These are not validated by render-time CI — grep the rendered manifests
  for every `secretRef`/`secretKeyRef` and projected `items[].key` before applying. The CNPG
  superuser fields (`POSTGRES_SUPER_USER`/`POSTGRES_SUPER_PASS`) come from the existing
  `cloudnative-pg` 1Password item; if `POSTGRES_SUPER_USER` is absent, hardcode
  `INIT_POSTGRES_SUPER_USER: postgres`.
- **Secrets must live in the `Talos` 1Password vault** (the ClusterSecretStore only reads `Talos`).
  Create the `scanopy` item via the `op` CLI, never the UI; `SCANOPY_NETWORK_ID` is a lowercase
  UUID, and `SNMP_COMMUNITY` is the one value supplied by hand.
- **`volsync` component lives in `ks.yaml` `spec.components` only** — not also in
  `app/kustomization.yaml`, or it double-applies. The PVC it creates is named `${APP}` (`scanopy`),
  referenced by the server's `persistence.data.existingClaim`.
- **hostNetwork port `60073`** is bound on every node's host IP — confirm nothing else uses it.
- **Daemon hostPath `/var/lib/scanopy-daemon`** holds daemon config; it persists across Talos
  reboots (`/var/lib` is writable + persistent).
- **Privileged is allowed** — the cluster enforces no restricted PodSecurity and the `network`
  namespace has no PSA labels, so the privileged hostNetwork daemon admits cleanly.
- **Image digests are pinned** for both server and daemon (`v0.16.2`) and the `postgres-init`
  initContainer; Renovate manages bumps afterward.

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
  kubectl -n network logs deploy/scanopy -c init-db
  kubectl -n network logs deploy/scanopy -c app | grep -iE 'tls|ssl|database|connect'
  ```

- Confirm one daemon per node and that each registered:

  ```bash
  kubectl -n network get pods -l app.kubernetes.io/name=scanopy -o wide
  kubectl -n network logs ds/scanopy-daemon | grep -iE 'register|network|server'
  ```

- After deploy, in the UI: create an SNMP credential pointing at `/run/secrets/snmp-community` and
  confirm a switch/AP/router is discovered; trigger a test email (e.g. a user invite) and confirm it
  relays via the internal SMTP relay.
- The server container runs with no restrictive `securityContext` (writes `/data`, reads
  `/app/static`); harden to `runAsNonRoot`/`readOnlyRootFilesystem` only after confirming it stays up.
- Follow-ups out of scope for v1: OIDC/SSO and Prometheus metrics.
