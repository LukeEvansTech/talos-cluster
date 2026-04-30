# Oxidized Network Config Backup Design

## Overview

Deploy [Oxidized](https://github.com/ytti/oxidized) — a network device configuration backup tool — into the `observability` namespace, polling six homelab network devices daily and pushing config diffs to a private GitHub repo (`LukeEvansTech/network-configs`). Notifications go to Pushover (drift via Oxidized's `exec` hook; stale-device alerts via the existing kube-prometheus-stack → Alertmanager → Pushover route).

Purpose: drift detection. The Terraform/Ansible IaC in the sibling `network-ops` repo describes *desired* state; Oxidized snapshots *actual* running state, catching out-of-band changes (manual GUI/SSH edits) that bypass IaC.

## Scope

**In scope (devices polled):**

| Device | Hostname | Oxidized model |
|---|---|---|
| OPNsense firewall | `opnsense.lan` | `opnsense` |
| Mellanox SN2700 core switch | `onyx.lan` | `onyx` |
| MikroTik CRS354 PoE | `mikrotik-poe.lan` | `routeros` |
| MikroTik CRS354 non-PoE | `mikrotik-nonpoe.lan` | `routeros` |
| Ruckus AP/controller | `ruckus.lan` | `ruckusunleashed` *(model TBC during pre-deploy)* |

**Out of scope:**
- APC PDU — SNMP-only; no config-dump fit for Oxidized.
- Cloud services (NextDNS, Tailscale, Cloudflare) — already IaC-managed in Terraform Cloud.

## Architecture

Single-pod deployment in `observability`, sitting next to `snmp-exporter`. Oxidized polls devices on a 24h interval, writes to a local bare git repo on a Ceph PVC, and pushes commits to GitHub via a deploy key. A sidecar Prometheus exporter scrapes Oxidized's `/nodes.json` and exposes per-device last-success metrics.

```
                              ┌────────────────────────────────────────┐
                              │  GitHub: LukeEvansTech/network-configs │
                              │           (private)                    │
                              └──────────────▲─────────────────────────┘
                                             │ git push (SSH deploy key)
┌────────────────────────────────────────────┼─────────────────────────────┐
│ Talos cluster — namespace: observability   │                             │
│                                            │                             │
│  ┌─────────────────── oxidized pod ────────┴──────────────────┐          │
│  │  oxidized:0.36.0  (port 8888)                              │          │
│  │  oxidized-exporter:v1.0.5  (port 8080)                     │          │
│  │  initContainers:                                           │          │
│  │   - ssh-setup       (writes deploy key to tmpfs)           │          │
│  │   - router-db-render (envsubst → router.db on tmpfs)       │          │
│  │                                                            │          │
│  │  PVC (ceph-block, 1Gi, VolSync-protected):                 │          │
│  │    ~/.config/oxidized/  ← Oxidized state + bare git repo   │          │
│  │  emptyDir tmpfs:                                           │          │
│  │    ~/.ssh/                       ← deploy key (memory)     │          │
│  │    /etc/oxidized/router.db.d/    ← rendered creds (memory) │          │
│  │  ConfigMap (mounted on init only for router.db template):  │          │
│  │    /etc/oxidized/config                                    │          │
│  │  Secret (ExternalSecret → 1Password):                      │          │
│  │    device creds, GH deploy key, Pushover token             │          │
│  └────────┬─────────────┬───────────────┬─────────────────────┘          │
│           │             │               │                                │
│           ▼             ▼               ▼                                │
│  HTTPRoute        ServiceMonitor   exec hook → Pushover API              │
│  envoy-internal   → Prometheus      (drift + node_fail)                  │
│                          │                                               │
│                          ▼                                               │
│              PrometheusRule: stale device > 48h                          │
│                          │                                               │
│                          ▼                                               │
│              Alertmanager → Pushover (existing route)                    │
└─────────────────────────┼────────────────────────────────────────────────┘
                          │ SSH / HTTPS polls (every 24h)
                          ▼
   opnsense · onyx · mikrotik_poe · mikrotik_nonpoe · ruckus
```

## File Layout

```
kubernetes/apps/observability/oxidized/
  ks.yaml                    # Flux Kustomization → ./app
  app/
    kustomization.yaml       # Resource list + components: ../../../../components/volsync
    helmrelease.yaml         # bjw-s/app-template HelmRelease (oxidized + exporter sidecar)
    ocirepository.yaml       # OCI chart source (bjw-s app-template)
    externalsecret.yaml      # 1Password → Secret (creds, deploy key, Pushover)
    configmap.yaml           # Oxidized config (YAML) + router.db (CSV)
    httproute.yaml           # oxidized.${SECRET_DOMAIN} → envoy-internal
    prometheusrule.yaml      # OxidizedDeviceStale + OxidizedDown alerts
```

**One change to existing files:**
- `kubernetes/apps/observability/kustomization.yaml` — add `- ./oxidized` (alphabetical position between `nut-exporter` and `peanut`).

## Container Images

| Image | Tag | Purpose |
|---|---|---|
| `docker.io/oxidized/oxidized` | `0.36.0@sha256:<digest>` | Main app (resolved at implementation) |
| `ghcr.io/akquinet/oxidized-exporter` | `v1.0.5@sha256:<digest>` | Sidecar Prometheus exporter |

Both tracked by Renovate.

## Chart Source

`ocirepository.yaml`: `oci://ghcr.io/bjw-s-labs/helm/app-template` tag `4.6.2` (matches sibling apps).

## Flux Kustomization (`ks.yaml`)

- `dependsOn`: `rook-ceph-cluster` (for VolSync-backed PVC), `external-secrets-stores` (for `onepassword-connect` ClusterSecretStore).
- Components: `../../../../components/volsync`.
- `postBuild.substitute`:
  - `APP: oxidized`
  - `VOLSYNC_CAPACITY: 1Gi`
  - `GATUS_SUBDOMAIN: oxidized` *(if you want a Gatus check — optional)*

## HelmRelease Spec

### Controllers / Containers

```yaml
controllers:
  oxidized:
    initContainers:
      ssh-setup:
        image: busybox:1.37
        command: [/bin/sh, -c]
        args:
          - |
            install -m 0700 -d /ssh-out
            printf '%s\n' "$GITHUB_DEPLOY_KEY" > /ssh-out/id_ed25519
            chmod 0600 /ssh-out/id_ed25519
            ssh-keygen -y -f /ssh-out/id_ed25519 > /ssh-out/id_ed25519.pub
            printf '%s\n' "$GITHUB_KNOWN_HOSTS" > /ssh-out/known_hosts
            chmod 0644 /ssh-out/known_hosts /ssh-out/id_ed25519.pub
        env:
          GITHUB_DEPLOY_KEY: { secretKeyRef: { name: oxidized-secret, key: GITHUB_DEPLOY_KEY } }
          GITHUB_KNOWN_HOSTS: { secretKeyRef: { name: oxidized-secret, key: GITHUB_KNOWN_HOSTS } }
      router-db-render:
        image: docker.io/alpine:3.21
        command: [/bin/sh, -c]
        args:
          - |
            apk add --no-cache gettext >/dev/null
            envsubst < /etc/oxidized-tmpl/router.db > /router-out/router.db
            chmod 0600 /router-out/router.db
        env:
          # all *_USERNAME / *_PASSWORD vars from oxidized-secret
          OPNSENSE_USERNAME: { secretKeyRef: { name: oxidized-secret, key: OPNSENSE_USERNAME } }
          OPNSENSE_PASSWORD: { secretKeyRef: { name: oxidized-secret, key: OPNSENSE_PASSWORD } }
          ONYX_USERNAME:     { secretKeyRef: { name: oxidized-secret, key: ONYX_USERNAME } }
          ONYX_PASSWORD:     { secretKeyRef: { name: oxidized-secret, key: ONYX_PASSWORD } }
          MIKROTIK_POE_USERNAME:    { secretKeyRef: { name: oxidized-secret, key: MIKROTIK_POE_USERNAME } }
          MIKROTIK_POE_PASSWORD:    { secretKeyRef: { name: oxidized-secret, key: MIKROTIK_POE_PASSWORD } }
          MIKROTIK_NONPOE_USERNAME: { secretKeyRef: { name: oxidized-secret, key: MIKROTIK_NONPOE_USERNAME } }
          MIKROTIK_NONPOE_PASSWORD: { secretKeyRef: { name: oxidized-secret, key: MIKROTIK_NONPOE_PASSWORD } }
          RUCKUS_USERNAME:          { secretKeyRef: { name: oxidized-secret, key: RUCKUS_USERNAME } }
          RUCKUS_PASSWORD:          { secretKeyRef: { name: oxidized-secret, key: RUCKUS_PASSWORD } }
    containers:
      app:
        image:
          repository: oxidized/oxidized
          tag: 0.36.0@sha256:<digest>
        env:
          # only Pushover creds at runtime; device creds were baked into router.db by the init
          PUSHOVER_TOKEN:    { secretKeyRef: { name: oxidized-secret, key: PUSHOVER_TOKEN } }
          PUSHOVER_USER_KEY: { secretKeyRef: { name: oxidized-secret, key: PUSHOVER_USER_KEY } }
        probes:
          liveness:  { type: TCP, port: 8888 }
          readiness: { type: HTTP, path: /nodes.json, port: 8888 }
        resources:
          requests: { cpu: 50m, memory: 128Mi }
          limits:   { memory: 256Mi }
        securityContext:
          runAsNonRoot: true
          runAsUser: 30000
          runAsGroup: 30000
          readOnlyRootFilesystem: true
          allowPrivilegeEscalation: false
          capabilities: { drop: [ALL] }
          seccompProfile: { type: RuntimeDefault }
      exporter:
        image:
          repository: ghcr.io/akquinet/oxidized-exporter
          tag: v1.0.5@sha256:<digest>
        args: ["-U", "http://localhost:8888"]   # exporter listens on :8080 by default
        resources:
          requests: { cpu: 10m, memory: 32Mi }
          limits:   { memory: 64Mi }
        securityContext: { ...same hardening as app... }
```

### Service & Routing

```yaml
service:
  app:
    controller: oxidized
    ports:
      http:    { port: 8888 }
      metrics: { port: 8080 }

serviceMonitor:
  app:
    serviceName: oxidized
    endpoints:
      - port: metrics
        interval: 30s
        scrapeTimeout: 10s
```

`httproute.yaml`:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: oxidized
spec:
  hostnames: [oxidized.${SECRET_DOMAIN}]
  parentRefs: [{ name: envoy-internal, namespace: network }]
  rules:
    - backendRefs: [{ name: oxidized, port: 8888 }]
```

### Persistence

```yaml
persistence:
  config:
    type: persistentVolumeClaim
    storageClass: ceph-block
    accessMode: ReadWriteOnce
    size: 1Gi
    advancedMounts:
      oxidized:
        app:        [{ path: /home/oxidized/.config/oxidized }]
  ssh:
    type: emptyDir
    medium: Memory
    advancedMounts:
      oxidized:
        ssh-setup:  [{ path: /ssh-out }]
        app:        [{ path: /home/oxidized/.ssh, readOnly: true }]
  router-db:
    type: emptyDir
    medium: Memory
    advancedMounts:
      oxidized:
        router-db-render: [{ path: /router-out }]
        app:              [{ path: /etc/oxidized/router.db.d, readOnly: true }]
  oxidized-config:
    type: configMap
    name: oxidized-config
    advancedMounts:
      oxidized:
        app:
          - { subPath: config, path: /etc/oxidized/config }
        router-db-render:
          - { subPath: router.db, path: /etc/oxidized-tmpl/router.db }
  tmp:
    type: emptyDir
    advancedMounts:
      oxidized:
        app:        [{ path: /tmp }]
```

VolSync component handles snapshots of the `config` PVC.

## ConfigMap

### `config` (Oxidized YAML)

```yaml
username: placeholder            # router.db provides per-device user/pass
password: placeholder
model: routeros
interval: 86400                  # 24h
log: /home/oxidized/.config/oxidized/oxidized.log
debug: false
threads: 6
timeout: 30
retries: 2
rest: 0.0.0.0:8888               # required for HTTPRoute, exporter, /nodes.json probe
prompt: !ruby/regexp /^([\w.@-]+[#>]\s?)$/

input:
  default: ssh
  ssh:
    secure: false

output:
  default: git
  git:
    user: Oxidized
    email: oxidized@${SECRET_DOMAIN}
    repo: /home/oxidized/.config/oxidized/devices.git

source:
  default: csv
  csv:
    file: /etc/oxidized/router.db.d/router.db
    delimiter: !ruby/regexp /:/
    map:
      name:     0
      ip:       1
      model:    2
      username: 3
      password: 4

hooks:
  push_to_github:
    type: githubrepo
    events: [post_store]
    remote_repo: git@github.com:LukeEvansTech/network-configs.git
    publickey_file: /home/oxidized/.ssh/id_ed25519.pub
    privatekey_file: /home/oxidized/.ssh/id_ed25519
  pushover_drift:
    type: exec
    events: [post_store]
    cmd: |
      curl -s --form-string "token=$PUSHOVER_TOKEN" \
              --form-string "user=$PUSHOVER_USER_KEY" \
              --form-string "title=Oxidized: config drift" \
              --form-string "message=$OX_NODE_NAME ($OX_NODE_MODEL) changed" \
              https://api.pushover.net/1/messages.json
  pushover_fail:
    type: exec
    events: [node_fail]
    cmd: |
      curl -s --form-string "token=$PUSHOVER_TOKEN" \
              --form-string "user=$PUSHOVER_USER_KEY" \
              --form-string "title=Oxidized: poll failed" \
              --form-string "priority=1" \
              --form-string "message=$OX_NODE_NAME ($OX_NODE_MODEL) — $OX_NODE_MSG" \
              https://api.pushover.net/1/messages.json
```

### `router.db` template (CSV — `name:ip:model:username:password`)

The ConfigMap holds a *template* with `${VAR}` placeholders. The `router-db-render` init container reads it from `/etc/oxidized-tmpl/router.db`, runs `envsubst` against env vars sourced from `oxidized-secret`, and writes the rendered file to a tmpfs `emptyDir` (`/router-out`). The main container mounts that tmpfs read-only at `/etc/oxidized/router.db.d/router.db`. Net result: device credentials never land on the Ceph PVC and never appear in the ConfigMap.

Template content:

```
opnsense:opnsense.lan:opnsense:${OPNSENSE_USERNAME}:${OPNSENSE_PASSWORD}
onyx:onyx.lan:onyx:${ONYX_USERNAME}:${ONYX_PASSWORD}
mikrotik_poe:mikrotik-poe.lan:routeros:${MIKROTIK_POE_USERNAME}:${MIKROTIK_POE_PASSWORD}
mikrotik_nonpoe:mikrotik-nonpoe.lan:routeros:${MIKROTIK_NONPOE_USERNAME}:${MIKROTIK_NONPOE_PASSWORD}
ruckus:ruckus.lan:ruckusunleashed:${RUCKUS_USERNAME}:${RUCKUS_PASSWORD}
```

## ExternalSecret

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: oxidized
spec:
  secretStoreRef: { kind: ClusterSecretStore, name: onepassword-connect }
  target:
    name: oxidized-secret
    template:
      engineVersion: v2
      data:
        OPNSENSE_USERNAME:    "{{ .OPNSENSE_USERNAME }}"
        OPNSENSE_PASSWORD:    "{{ .OPNSENSE_PASSWORD }}"
        ONYX_USERNAME:        "{{ .ONYX_USERNAME }}"
        ONYX_PASSWORD:        "{{ .ONYX_PASSWORD }}"
        MIKROTIK_POE_USERNAME:    "{{ .MIKROTIK_POE_USERNAME }}"
        MIKROTIK_POE_PASSWORD:    "{{ .MIKROTIK_POE_PASSWORD }}"
        MIKROTIK_NONPOE_USERNAME: "{{ .MIKROTIK_NONPOE_USERNAME }}"
        MIKROTIK_NONPOE_PASSWORD: "{{ .MIKROTIK_NONPOE_PASSWORD }}"
        RUCKUS_USERNAME:      "{{ .RUCKUS_USERNAME }}"
        RUCKUS_PASSWORD:      "{{ .RUCKUS_PASSWORD }}"
        GITHUB_DEPLOY_KEY:    "{{ .GITHUB_DEPLOY_KEY }}"
        GITHUB_KNOWN_HOSTS:   "{{ .GITHUB_KNOWN_HOSTS }}"
        PUSHOVER_TOKEN:       "{{ .PUSHOVER_TOKEN }}"
        PUSHOVER_USER_KEY:    "{{ .PUSHOVER_USER_KEY }}"
  dataFrom:
    - extract: { key: oxidized }
```

**1Password item to create**: `oxidized` in the **`Talos`** vault (the only vault read by `ClusterSecretStore onepassword-connect`), with the 14 fields above. Source values for MikroTik / Ruckus / OPNsense live in `Home Operations` and must be copied across via `op read` when creating the item. The user is responsible for creating this item before deploy.

## PrometheusRule

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: oxidized
spec:
  groups:
    - name: oxidized
      rules:
        - alert: OxidizedDeviceStale
          # 2=success, 1=never, 0=no_connection per akquinet/oxidized-exporter
          expr: oxidized_device_status != 2
          for: 48h
          labels: { severity: warning }
          annotations:
            summary: "Oxidized has not successfully polled {{ $labels.full_name }} in >48h"
        - alert: OxidizedDown
          expr: up{job=~".*oxidized.*"} == 0
          for: 15m
          labels: { severity: warning }
          annotations:
            summary: "Oxidized exporter is down"
```

Metric semantics confirmed against the akquinet/oxidized-exporter source: `oxidized_device_status` is a gauge labeled `full_name`, `name`, `group`, `model` with values `2=success / 1=never / 0=no_connection`. Alert fires only after 48h of continuously not-success (with daily polls, that's at least two consecutive failed cycles).

## Preconditions

Before deploy succeeds, the following must be true:

| Precondition | How to verify |
|---|---|
| Pod can reach each device's hostname/port | `kubectl run -it --rm netshoot --image=nicolaka/netshoot -- nc -zv <device>.lan 22` |
| OPNsense Unbound resolves `*.lan` for cluster | DNS query from a worker pod |
| GitHub repo `LukeEvansTech/network-configs` exists, private | `gh repo view LukeEvansTech/network-configs` |
| Deploy key with **write** access added to repo | `gh repo deploy-key list --repo LukeEvansTech/network-configs` |
| 1Password item `oxidized` populated with all 14 fields | Manual check |
| Each device's credentials work via direct SSH/API | Manual login test |
| OPNsense firewall rule permitting cluster pod CIDR → device VLAN ports (SSH 22, HTTPS 443) | OPNsense rule check |

If any precondition fails, the implementation plan defines specific recovery steps.

## Failure Modes

| Failure | Detection | Recovery |
|---|---|---|
| Single device unreachable | `node_fail` hook → Pushover; `OxidizedDeviceStale` alert after 48h | Self-heals when device returns |
| GitHub push fails | Logs + `node_fail` event | Local commits accumulate; flush on next push |
| PVC corrupted | Pod CrashLoopBackOff | Delete PVC → VolSync restore, **or** delete PVC and re-clone from GitHub |
| 1Password Connect down | ExternalSecret stale | Already-mounted Secret keeps working; tolerable for hours |
| Pushover outage | Hook curl exits non-zero | Drift commits still stored in git; only notification lost |
| Wrong Ruckus model | That device's polls fail | Swap `model:` in router.db, redeploy ConfigMap |

## Security Posture

- All containers: `runAsNonRoot`, `runAsUser: 30000`, `readOnlyRootFilesystem`, `drop: [ALL]`, `seccompProfile: RuntimeDefault`.
- SSH deploy key lives only on `tmpfs` (memory), never the Ceph PVC.
- Deploy key scope: write to `LukeEvansTech/network-configs` only.
- GitHub repo private. Configs may contain topology/IP info even after `remove_secret` filters.
- NetworkPolicy: egress to device VLAN ports (22, 443), `github.com:22`, `api.pushover.net:443`, kube-dns; deny everything else.

## Testing & Validation Plan

**Pre-deploy:**
1. Manually SSH/API each of 6 devices using the credentials staged in 1Password.
2. Confirm GitHub repo + deploy key.
3. `flux-local build ks --path kubernetes/flux/cluster oxidized` for CI parity.

**Initial deploy (validation branch):**
1. Set `interval: 60` (1-min) temporarily for fast feedback.
2. `just kube apply-ks observability oxidized`.
3. Watch pod logs: 6 successful logins + 6 initial commits.
4. Browse Oxidized UI via HTTPRoute; all 6 devices green.
5. Confirm GitHub repo shows 6 config files.
6. Manually edit one device (e.g., add a comment in MikroTik) → wait for next poll → verify git diff + Pushover message.
7. Stop SSH on one device temporarily; confirm `node_fail` hook fires (Pushover notification).
8. Lower alert `for:` threshold and confirm `OxidizedDeviceStale` fires in Alertmanager.

**Pre-merge cleanup:**
1. Restore `interval: 86400`.
2. Restore alert thresholds.
3. Merge to main.

## Rollback / Uninstall

```bash
just kube delete-ks observability oxidized
kubectl delete pvc -n observability oxidized
# 1Password item + GitHub repo remain — clean up manually if abandoning permanently
```

The git history on GitHub is durable evidence even after uninstall — redeploy picks up where left off.

## Open Items (resolved during implementation plan)

- **Ruckus model choice** — confirm Unleashed vs SmartZone vs ZoneDirector before populating `router.db`.
- **OPNsense secret stripping** — verify after first poll whether the committed `config.xml` contains plaintext secrets; add `remove_secret` filter and any custom regex if so.
- **Container image digests** — pinned at implementation time.
- **NetworkPolicy egress rules** — exact device IPs/CIDRs.

## References

- Oxidized: <https://github.com/ytti/oxidized>
- Oxidized hooks docs: <https://github.com/ytti/oxidized/blob/master/docs/Hooks.md>
- akquinet/oxidized-exporter: <https://github.com/akquinet/oxidized-exporter>
- Pushover API: <https://pushover.net/api>
- Reference blog post that prompted this work: <https://oneuptime.com/blog/post/2026-02-08-how-to-run-oxidized-in-docker-for-network-config-backup/view>
- Sibling app pattern: `kubernetes/apps/observability/snmp-exporter/`
