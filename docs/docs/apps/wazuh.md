# Wazuh XDR/SIEM

## Purpose

Wazuh is the cluster's XDR/SIEM stack, in the `security` namespace
(`kubernetes/apps/security/wazuh`): an indexer (OpenSearch), a manager (agent enrolment, syslog
ingest, correlation), and a dashboard, receiving syslog from the firewall, hypervisors and network
kit plus events from enrolled agents such as the NAS.

## Vendored, not consumed

The manifests are vendored from `wazuh/wazuh-kubernetes`, not consumed as a chart: upstream ships
no Helm chart, and its Kustomize base carries defects that have to be fixed regardless. Fixed here:

- A JVM heap set to 65% of the container's memory limit, which OOM-kills the indexer with no
  `OutOfMemoryError` in the log ([wazuh-kubernetes#879](https://github.com/wazuh/wazuh-kubernetes/issues/879)).
- 500Mi PVCs, far short of what full-fidelity ingest needs.
- A privileged `increase-the-vm-max-map-count` init container, rejected by this namespace's Pod
  Security baseline.
- `WAZUH_API_URL` pointed at a StatefulSet pod hostname that is wrong for the whole 4.x line
  ([wazuh-kubernetes#902](https://github.com/wazuh/wazuh-kubernetes/issues/902)).
- No index lifecycle policy, so alert indices grow until the volume fills.
- OpenSearch's public demo password hashes shipped in `internal_users.yml`.

A `renovate: datasource=github-tags` marker in `kustomization.yaml` tracks upstream's manifest
releases and opens a PR when one lands, since no tool can diff manifest shape for us; re-vendoring
stays a manual, reviewed step.

## Design decisions

- **Single-node OpenSearch** (`discovery.type: single-node`). Upstream's default 3-replica set has
  `wazuh-indexer-0` as both the only seed host and the only bootstrap master, so losing its volume
  strands the other two nodes without a quorum. Single-node removes quorum from the picture.
- **Manager resources** (500m/2Gi request, 4Gi limit) are sized for full-fidelity syslog ingest,
  about 14M events/day observed on the existing collector, not upstream's 400m/512Mi demo sizing,
  which is undersized for a container running analysisd, remoted, wazuh-db, the API and Filebeat
  together.
- **Manager capabilities** are the container runtime's default set minus `NET_RAW`, `MKNOD` and
  `AUDIT_WRITE`, restored on top of `drop: ALL`. The set is empirically determined: the entrypoint
  chroots, chowns and steps down to the `wazuh` user, and a smaller set failed in testing (without
  `FOWNER`, `wazuh-clusterd` dies on `chmod` of `cluster.log`; trimming further breaks
  `wazuh-apid`). Re-verify against the image before trimming it further.
- **No `reloader.stakater.com/auto`** on the manager, indexer or dashboard. Auto would also watch
  the `wazuh` Secret, which carries both client passwords and the `internal_users.yml` hashes, so a
  credential rotation would restart every pod onto the new plaintext passwords the moment the
  ExternalSecret refreshed, while the indexer's security index still held the old hashes. See
  [credential rotation](#credential-rotation) below for why that race matters and how it is avoided.

## Deploy prerequisite: vm.max_map_count

The indexer's `increase-the-vm-max-map-count` init container is removed (it needs `privileged:
true`, which the namespace's Pod Security baseline rejects); `vm.max_map_count=262144` is set on
the nodes instead, by `talos/patches/global/machine-sysctls.yaml`. That patch is an input to `just
talos gen-config` / `apply-node`, and Flux does not push machine config to nodes, so it must be
applied to every schedulable node **before** this app is enabled. Confirm it landed:

```bash
talosctl -n <node> read /proc/sys/vm/max_map_count   # expect 262144
```

`discovery.type: single-node` happens to skip OpenSearch's own bootstrap check for this value, so
a missed node does not fail cleanly at startup. The symptom is mmap exhaustion under load later.

## Dashboard readiness probe

The canonical OpenSearch Dashboards health endpoint, `/api/status`, is authenticated by the
security plugin. A kubelet probe cannot read a Secret, so it gets a 401 and the kubelet restarts a
perfectly healthy pod in a loop: 128 restarts in 11 hours were observed while the dashboard kept
serving the UI throughout. `/app/login` is served unauthenticated and returns 200 only once every
plugin, including the one issuing that 401, has finished loading, which makes it a stronger
readiness signal than the 302 on `/`. Verified against the running pod:

```text
/api/status 401 | /status 401 | / 302 | /app/login 200
```

## Networking

- **`wazuh-syslog`'s LoadBalancer uses `externalTrafficPolicy: Cluster`, not `Local`.** Cilium's L2
  announcement elects a lease-holder node without regard to backend locality, so under `Local` the
  LB address is ARP'd by a node that refuses the traffic whenever the pod sits elsewhere: external
  senders get a TCP RST or silent UDP loss, while in-cluster traffic keeps working. That cost days
  of missing firewall logs on the sentinel-syslog service
  ([#4070](https://github.com/LukeEvansTech/talos-cluster/pull/4070)). The cost of `Cluster` is
  SNAT: every sender arrives as a node address, which is why `ossec.conf` allow-lists a CIDR
  instead of per-device entries and `authd` sets `use_source_ip` to `no`.
- **The `CiliumNetworkPolicy` is required, not optional.** The cluster runs default-deny ingress,
  and external senders arrive with Cilium's `world` identity, so without this policy every
  LoadBalancer'd port is dropped at pod ingress. The manager comes up healthy and simply receives
  nothing, the worst failure mode for a log collector. It is scoped to the ingest ports only, never
  the `:55000` API; `world`, not a CIDR, keeps LAN addresses out of this public repository.
- **Port 1514 is declared explicitly on the manager StatefulSet.** Upstream puts the agent event
  channel only on the worker StatefulSet; a master-only deployment that omits it lets agents
  register successfully and then never connect.

## Credential rotation

Rotating the passwords in 1Password is not sufficient on its own. OpenSearch Security reads
`internal_users.yml` only while it initialises its persisted security index
(`allow_default_init_securityindex`, which fires when that index does not yet exist); a restart
against an existing index does not re-import the file. On a naive rotation the manager and
dashboard pick up the new plaintext passwords from the refreshed Secret while the indexer still
accepts only the old hashes, and every component fails to authenticate.

The order matters, and step 3 is the non-obvious one:

1. Update the passwords in the 1Password item.
2. Wait for the ExternalSecret to refresh, or force it:
   `kubectl annotate es wazuh -n security force-sync=$(date +%s) --overwrite`.
3. Restart the indexer first, and wait for it:
   `kubectl rollout restart sts/wazuh-indexer -n security`, then
   `kubectl rollout status sts/wazuh-indexer -n security`. The `status` call is not optional:
   `rollout restart` returns as soon as the restart is requested, so without it step 4 can exec
   into the old pod, which is still terminating, and import its stale file. `internal_users.yml`
   is mounted with `subPath`, which Kubernetes never refreshes in a running container, so without
   this wait step 4 would faithfully re-import the old hashes and the rotation would silently
   no-op. The restart alone imports nothing either: the security index already exists, so
   `allow_default_init_securityindex` does not fire.
4. Import the new hashes from the now-current file:

   ```bash
   kubectl exec -n security sts/wazuh-indexer -- env \
     JAVA_HOME=/usr/share/wazuh-indexer/jdk \
     OPENSEARCH_PATH_CONF=/usr/share/wazuh-indexer/config \
     bash /usr/share/wazuh-indexer/plugins/opensearch-security/tools/securityadmin.sh \
     -f /usr/share/wazuh-indexer/config/opensearch-security/internal_users.yml \
     -t internalusers -nhnv -p 9300 \
     -cacert /usr/share/wazuh-indexer/config/certs/root-ca.pem \
     -cert   /usr/share/wazuh-indexer/config/certs/admin.pem \
     -key    /usr/share/wazuh-indexer/config/certs/admin-key.pem
   ```

   `-nhnv` is required: the certs carry SANs, but transport hostname verification is disabled,
   matching `opensearch.yml`.

5. Only now move the clients onto the new passwords:
   `kubectl rollout restart sts/wazuh-manager deploy/wazuh-dashboard -n security`.

Between steps 3 and 5 the manager and dashboard are still using the old passwords, which the
indexer still accepts until step 4 lands, so the stack keeps working throughout rather than
breaking mid-procedure.

## References

- `kubernetes/apps/security/wazuh/app/externalsecret.yaml`, the credential Secret and the
  `internal_users.yml` template.
- `kubernetes/apps/security/wazuh/app/indexer.yaml`, `manager.yaml`, `dashboard.yaml`, the
  workloads this page's design decisions apply to.
- `kubernetes/apps/security/wazuh/app/services.yaml`, `networkpolicy.yaml`, the syslog LoadBalancer
  and the default-deny ingress rule.
- `talos/patches/global/machine-sysctls.yaml`, the `vm.max_map_count` node prerequisite.
