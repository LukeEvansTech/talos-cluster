# Nutify

UPS monitoring web UI in the `observability` namespace
([DartSteven/Nutify](https://github.com/DartSteven/Nutify)) — a Flask + SQLite front end over
Network UPS Tools, giving dashboards, historical telemetry, and scheduled reports for the UPSes
served by the external NUT appliance.

## Purpose

- **Human-facing layer, not the alerting path.** Paging still runs through
  `nut-exporter` → Prometheus → Alertmanager → Pushover (`UPSOutputOff`, `UPSMetricsMissing`,
  `UPSUnreachable`, the battery/load rules). Nutify adds charts, energy/cost views, and emailed
  reports on top of the same data. If Nutify is down, nothing about alerting changes — treat it as
  optional.
- **NUT client only.** The appliance that talks to the UPS management cards is out of cluster (it
  has to outlive the cluster it shuts down, see the `nut-exporter` ServiceMonitor). Nutify is
  configured in "Remote NUT Only" topology and runs no drivers, no `upsd`, and no USB stack.
- Internal-only: exposed on `envoy-internal` at `nutify.${SECRET_DOMAIN}`.

## Design decisions

- **Root, but no privileges.** Upstream's compose sets `privileged: true` plus `SYS_ADMIN`,
  `SYS_RAWIO`, `MKNOD`, a `/dev` bind and `device_cgroup_rules` — all of that is the USB-attached-UPS
  path. As a remote client none of it applies, so the pod only sets `runAsNonRoot: false`. Root is
  still required: PID 1 is a bash entrypoint that creates and chowns `/var/run/nut`, `/var/log/nut`
  and `/etc/nut` before starting the Flask app in the foreground, with no privilege drop.
- **`SKIP_PERMCHECK: "true"`.** Without it the entrypoint `chown -R root:nut /dev/bus/usb` (harmless
  warning when absent) and sets the suid bit on `upsc`/`upscmd`/`upsrw`. Neither is wanted here.
- **`ENABLE_LOG_STARTUP` is deliberately left unset.** Setting it to `Y` makes
  `start-services.sh` dump the whole environment — `SECRET_KEY` included — to stdout, which would
  put the key in VictoriaLogs. Unset, the startup banner and summary still print and only the
  post-boot chatter goes to `/dev/null`. Set it temporarily when debugging a boot failure, then
  remove it.
- **`UDEV` is not set.** The upstream compose passes `UDEV=1`, but nothing in the image's scripts or
  Python reads it — it is a leftover from an older base image.
- **`SSL_ENABLED: "false"`.** TLS terminates at the gateway. This also short-circuits the
  entrypoint's self-signed certificate generation, so `/app/ssl` needs no volume.
- **Arch-suffixed image tag.** Upstream publishes `latest-amd64`, `latest-raspberrypi5-arm64` and so
  on rather than a multi-arch index, so the tag has to name the architecture: `0.2.0-amd64`,
  digest-pinned as usual. The scheme flipped at 0.2.0 (`amd64-0.1.7.1` → `0.2.0-amd64`), so Renovate
  only compares forward from here; the older prefix-style tags are invisible to it.
- **`SECRET_KEY` is data-bearing, not a session salt.** It is the key Nutify encrypts stored
  credentials with (the remote NUT monitor password, SMTP auth). Rotating it makes everything already
  in the SQLite DB undecryptable. It lives in the `nutify` item in the `Talos` 1Password vault and
  reaches the pod through an ExternalSecret + `envFrom`.
- **Persistence is split by what is worth backing up.** One `ceph-block` PVC (`volsync`, 5Gi) holds
  `/app/nutify/instance` (the SQLite DB: telemetry history, users, encrypted credentials) and
  `/etc/nut` (the config the wizard writes; empty in the image, so mounting over it hides nothing).
  `/app/nutify/logs` is an `emptyDir` — file logs are not worth a snapshot. `/app/nutify/config`
  needs nothing: `settings.txt` is regenerated from the environment on every start.
- **VolSync mover UID 1000 matches the image.** The `nut` user is `uid=1000 gid=1000`, and the
  entrypoint chowns the DB and its directory to it, so the component defaults need no override.

## Deploy gotchas

- **Configuration is wizard-only — it is not GitOps.** There is no documented env var or config file
  to preseed the topology, the remote NUT host, or the admin account; the setup wizard writes them
  into the SQLite DB on first boot. That means **the PVC is the configuration**. Two consequences: a
  restore from VolSync is the only way to recover the setup, and a `SECRET_KEY` change is
  functionally a rebuild.
- **The remote NUT address is entered in the UI, never committed.** The appliance address lives in
  the `cluster-secrets` 1Password item for the ServiceMonitor's benefit; nothing about Nutify puts it
  in git. Keep it that way — this repository is public.
- **Reading NUT variables is anonymous, but `upsmon` is not.** If the wizard asks for monitor
  credentials, they come from the appliance's own `upsd.users`, not from anything in this repo.
- **0.2.0 is labelled "Internal Testing" upstream.** Its changelog entry carries that marker and it
  has been the newest tag since 2026-03-28. It is the release that has the multi-UPS runtime and the
  remote-topology wizard, which is exactly what this deployment needs, so it is the right choice —
  but treat instability as expected, and remember the alerting path does not depend on it.
- **Probes are TCP, not HTTP.** After setup, `/` redirects to the login page; a TCP check on 5050
  sidesteps having to reason about which status the app returns in which state.
- **First boot needs a long startup budget.** The SQLite schema is created before the listener opens,
  so the startup probe is widened to `failureThreshold: 30` / `periodSeconds: 10` (the 30s default is
  not enough).

## Operational notes

- Check the Kustomization, HelmRelease, and ExternalSecret:

  ```bash
  flux -n observability get kustomization nutify
  flux -n observability get helmrelease nutify
  kubectl -n observability get externalsecret nutify
  ```

- Confirm the pod is up and reached the summary banner:

  ```bash
  kubectl -n observability get pods -l app.kubernetes.io/name=nutify
  kubectl -n observability logs deploy/nutify
  ```

- Complete the setup wizard at `https://nutify.${SECRET_DOMAIN}`: choose the **Remote NUT Only**
  topology, point it at the external NUT appliance on port `3493`, add both UPSes, and create the
  admin account. Nothing works before this step, and none of it is reproducible from git.
- Verify the backup actually captured the configured state after the wizard:

  ```bash
  kubectl -n observability get replicationsource nutify nutify-r2
  ```

- Gatus picks the HTTPRoute up automatically. If the uptime check flaps on the login redirect,
  annotate the route rather than weakening the app.
