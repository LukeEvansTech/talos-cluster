# Known Issues

This document tracks known issues and their workarounds in the cluster.

## 1Password Connect PushSecret False 400 Errors

### Issue

PushSecret resources generate spurious HTTP 400 errors in logs despite successfully syncing secrets to 1Password:

```text
Warning: Errored
set secret failed: could not write remote ref tls.key to target secretstore onepassword-connect:
error updating 1Password Item: status 400: Unable to update item "codelooks-com-production-tls"
in Vault "w7oprzm4euz5yajs6gnje7bpzu"
```

However, checking the PushSecret status shows it's actually working:

```bash
$ kubectl get pushsecret codelooks-com-production-tls -n cert-manager
NAME                           AGE   STATUS
codelooks-com-production-tls   90d   Synced
```

### Root Cause

This is a **known bug** in 1Password Connect starting from version 1.7.3+:

- [External Secrets Issue #3631](https://github.com/external-secrets/external-secrets/issues/3631)
- 1Password Connect returns HTTP 400 errors even when updates succeed
- The External Secrets Operator correctly reports the PushSecret as `Synced: True`
- The errors are cosmetic noise from 1Password Connect itself

### Affected Versions

- **1Password Connect**: 1.7.3+ (including 1.8.1 currently deployed)
- **External Secrets Operator**: 0.9.19+
- **Working version**: 1Password Connect 1.15.0

### Current Status

- Secrets ARE syncing successfully to 1Password
- The 400 errors are false positives and can be safely ignored
- No functional impact on cert-manager or PushSecret operations

### Workarounds

#### Option 1: Ignore the Errors (Recommended)

The errors are harmless. Verify PushSecret is working:

```bash
kubectl get pushsecret -A
kubectl describe pushsecret <name> -n <namespace> | grep -A 5 "Status:"
```

If status shows `Synced: True`, the secret is successfully pushed to 1Password.

#### Option 2: Downgrade 1Password Connect

Downgrade to the last known working version:

```yaml
# kubernetes/apps/external-secrets/onepassword-connect/app/helmrelease.yaml
spec:
    values:
        connect:
            api:
                image:
                    repository: ghcr.io/1password/connect-api
                    tag: 1.15.0
            sync:
                image:
                    repository: ghcr.io/1password/connect-sync
                    tag: 1.15.0
```

#### Option 3: Restart 1Password Connect Periodically

Temporary workaround that clears errors for a few days:

```bash
kubectl rollout restart deployment onepassword-connect -n external-secrets
```

### Verification

To verify secrets are actually syncing to 1Password:

1. Check PushSecret status:

    ```bash
    kubectl describe pushsecret <name> -n <namespace>
    ```

2. Look for `Synced Push Secrets` section showing successful sync

3. Verify in 1Password vault that the item exists and contains current data

4. Check External Secrets Operator logs for actual errors vs. noise:

    ```bash
    kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=50
    ```

### Example Configuration

Working PushSecret configuration for cert-manager TLS certificates:

```yaml
---
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
    name: &name "${SECRET_DOMAIN/./-}-production-tls"
spec:
    secretStoreRefs:
        - name: onepassword-connect
          kind: ClusterSecretStore
    selector:
        secret:
            name: *name
    template:
        engineVersion: v2
        data:
            tls.crt: '{{ index . "tls.crt" | b64enc }}'
            tls.key: '{{ index . "tls.key" | b64enc }}'
    data:
        - match:
              secretKey: &key tls.crt
              remoteRef:
                  remoteKey: *name
                  property: *key
        - match:
              secretKey: &key tls.key
              remoteRef:
                  remoteKey: *name
                  property: *key
```

### References

- [External Secrets GitHub Issue #3631](https://github.com/external-secrets/external-secrets/issues/3631)
- [1Password Connect Release Notes](https://app-updates.agilebits.com/product_history/Connect)
- [External Secrets PushSecret Documentation](https://external-secrets.io/latest/api/pushsecret/)

### Resolution Status

⏳ **Pending upstream fix** - Monitoring issue #3631 for resolution from 1Password Connect team.

---

## Plex Direct-Play Buffering on LAN Apple TVs (BBR + MTU Probing)

### Issue

4K direct-play streams from Plex to Apple TVs on the wired LAN experience a
~60-second buffering pause every ~6 minutes. Pattern is independent of client
(affects both Apple TVs), independent of Wi-Fi vs wired, and does not affect
concurrent remote-WAN transcoded sessions.

From the player's perspective: playback freezes, client buffer drains, then
playback resumes once a new TCP connection has refilled the client buffer.

### Investigation (2026-04-17)

Ruled out (all clean under load):

- NFS media read path: `cr-storage-data:/mnt/pool/media` sustains 1.5 GB/s
- Rook-Ceph: `HEALTH_OK`, 121 pgs clean, nominal IOPS
- Node 25G NIC (`enp1s0np0` on cr-talos-02): 25-45 Mbps egress, no errors
- Cilium data plane: no drops against Apple TV flows (`cilium-dbg monitor`)
- GPU contention with Ollama: does not affect direct-play (no transcode)
- Switch / LAG path (MikroTik PoE → Mellanox SN2700 Po2): clean

Signal captured in `ss -tni` on the Plex pod, from a privileged `kubectl debug`
ephemeral container (netshoot + `netadmin` profile):

1. Every ~6 minutes a new TCP video socket opens (`src=:32400 dst=10.32.8.108:49xxx`,
   `rcvmss:1056` — the HLS 4K-segment signature) and starts direct-playing.
2. The Apple TV Plex client drives its receive window down toward zero
   (`snd_wnd` from `~130000` to `64` to `32`) and enters **persist mode**.
3. BBR paces at LAN-speed bandwidth estimates (~1 Gbps `pacing_rate` while
   actual `delivery_rate` is 6-10 Mbps) — bursts into the tiny window and
   experiences loss.
4. The retransmit/timeout bursts trip `tcp_mtu_probing=1` black-hole detection,
   which collapses the connection's MSS (`1448 → 758 → 128 → 64`).
5. Once MSS is tiny and `cwnd:1`, RTO backs off to 100+ seconds and the
   connection is effectively dead even though `ESTABLISHED`.
6. Apple TV silently opens a new 49xxx socket → playback resumes → cycle
   repeats at the 6-minute mark.

Key `ss -tni` fields from a dying socket (live evidence):

```text
ESTAB 0 3034256 :32400 10.32.8.108:49359
  bbr ... mss:64 pmtu:1500 cwnd:1 ssthresh:158
  bytes_sent:497577854 bytes_retrans:2380056
  retrans:1/1829 lost:133 rehash:9
  rto:104448 backoff:9
  rwnd_limited:571468ms(99.6%)
  pacing_rate:1040908216bps delivery_rate:27876584bps
  snd_wnd:189248 rcv_wnd:68608
```

Meanwhile parallel control-channel sockets to the same Apple TV IP stayed
healthy (`mss:1448`, `cwnd:250-320`, zero retrans) — confirming this is
workload-specific to the long-lived high-throughput video socket, not a
path-level problem.

### Root Cause

Two-part interaction:

1. **Plex explicitly sets BBR per-socket** via `setsockopt(TCP_CONGESTION, "bbr")`.
   This overrides any netns-default congestion control.
2. **BBR on sub-ms LAN RTT** estimates enormous bandwidth (mrtt ~0.15ms,
   bw ~1Gbps). When the receiver (tvOS Plex client) closes its receive window
   down to trickle levels, BBR's next probe after the window re-opens bursts
   at ~1 Gbps into a receiver that can't consume it. Losses occur at the
   Apple TV's receive ring buffer / socket buffer.
3. **`tcp_mtu_probing=1`** (Talos default) reacts to the burst losses by
   halving the MSS, then halving again — the classic TCP black-hole detection
   false-positive. Once MSS < 100, throughput collapses.

### Workarounds (Runtime, Ephemeral)

Applied via `nsenter` into the Plex pod's netns from a privileged node-debug
pod. Requires `NET_ADMIN` in the target netns, not node-wide:

```bash
# Find Plex PID on the node running the Plex pod
kubectl debug node/cr-talos-02 -it --image=nicolaka/netshoot:latest --profile=sysadmin
# Inside the debug pod:
PLEX_PID=$(pgrep -f "/plexmediaserver/Plex Media Server" | head -1)

# Fix 1: stop MSS collapse (confirmed: MSS held at 1448)
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_mtu_probing=0

# Fix 2: disallow BBR so Plex's setsockopt falls back to netns default
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_allowed_congestion_control="cubic reno"
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_congestion_control=cubic
```

Lost on pod restart.

### Permanent Fix Options

**Option 1 (recommended): Cilium pod-egress bandwidth annotation.**
Caps the pod's egress at the BPF EDT layer, which neutralizes BBR's LAN
burst pacing without touching TCP config. Treats the symptom at exactly the
right layer. Add to `kubernetes/apps/media/plex/app/helmrelease.yaml`:

```yaml
spec:
    values:
        controllers:
            plex:
                annotations:
                    # existing annotations...
                    kubernetes.io/egress-bandwidth: "200M"
```

Requires Cilium's `bandwidth-manager` which is already enabled in this cluster
(`BandwidthManager: EDT with BPF [BBR]` per `cilium status`).

**Option 2: Pod-level safeSysctls.**
Set the two sysctls at pod spec level so they survive restarts. Requires
adding these to the kubelet's `allowed-unsafe-sysctls` (Talos machine config
`.machine.kubelet.extraArgs`) — more invasive.

```yaml
spec:
    values:
        defaultPodOptions:
            securityContext:
                sysctls:
                    - name: net.ipv4.tcp_mtu_probing
                      value: "0"
                    - name: net.ipv4.tcp_allowed_congestion_control
                      value: "cubic reno"
```

### Verification

After applying, confirm no video socket MSS-collapses during a 15-minute
direct-play session:

```bash
# From a debug container in the Plex pod:
watch -n 10 "ss -tnoi 'dst <apple-tv-ip>' | grep -B1 'rcvmss:1056'"
```

Healthy signal: `mss` stays at `1448`, `cwnd` stays above ~50, `snd_wnd`
fluctuates but `retrans` count doesn't spike.

### References

- Linux TCP persist timer / zero-window probing: `net/ipv4/tcp_output.c`
- BBR on LAN discussion: <https://groups.google.com/g/bbr-dev> (search "LAN burst")
- Talos sysctl defaults: `/etc/sysctl.d/`
- Cilium BandwidthManager: <https://docs.cilium.io/en/stable/network/kubernetes/bandwidth-manager/>

### Resolution Status

✅ **Permanent fix applied via Option 1** (Cilium egress annotation,
`kubernetes.io/egress-bandwidth: "200M"`, commit `4a8086bb`, rolled
2026-04-17 22:33 UTC). Plex pod has been stable since — no restarts, no
user-reported flaps, no MSS-collapse signal in `ss -tni`. Option 2 (pod
sysctls via `allowed-unsafe-sysctls`) kept documented as a fallback if
Cilium's BandwidthManager is ever disabled.

---

## Plex Advertises Broken Connection URLs To plex.tv

### Issue

Intermittent "server unavailable" / mid-stream reconnects on LAN clients
(notably Apple TV) even though the Plex pod is healthy (0 restarts, 100%
readiness, sub-ms `/identity` probe RTT from another node on the LAN). No
pod restarts, no LB or HTTPRoute flaps, no node pressure.

Distinct from the BBR/MTU-probing buffering issue documented above — this
one manifests as connection drops and app-level "unavailable" states,
typically at session start or at session re-negotiation boundaries rather
than mid-stream 60-second freezes.

### Investigation (2026-04-17)

Captured in parallel during a reported flap:

- LAN-side `curl /identity` probe from a hostNetwork pod on a non-pod-host
  node: **zero failures**, sub-ms response, for the entire window.
- `talosctl pcap` on both the pod host and the L2 announcer node showed
  the Apple TV client opening TCP sockets to **two IPs simultaneously**:
  the Plex LoadBalancer IP (healthy) **and** the gateway LoadBalancer IP
  on port 32400 (repeated SYN retransmits, no SYN+ACK — gateway doesn't
  listen on 32400, only 443).
- Apple TV pcap showed `RST` bursts against the _healthy_ Plex socket
  exactly at the moment the user reported the drop. Not caused by server
  or network — client-side session teardown.
- Plex's own `Plex Media Server.log` at the drop timestamp:

    ```text
    ERROR - [Req#…/Transcode] Invalid value for 'location' client location attribute: unknown
    ERROR - [Req#…/Transcode] downloadContainer: expected MediaContainer element, found html
    ERROR - [Req#…/Transcode] TranscodeUniversalRequest: unable to get container: ///library/metadata/<id>?…
    ```

    Three tells in that trio: `location … unknown` (client's request URL
    didn't match any connection it considered LAN or WAN); HTML body where
    the transcoder expected XML (internal fetch hit a gateway error page);
    and the triple slash `///library/metadata/…` — a classic "empty host in
    URL-join" bug inside Plex's internal transcoder.

- Queried plex.tv for what the server is actually publishing:

    ```bash
    TOKEN=$(grep -oE 'PlexOnlineToken="[^"]+"' \
      '/config/Library/Application Support/Plex Media Server/Preferences.xml' \
      | cut -d\" -f2)
    curl -s 'https://plex.tv/api/v2/resources.json?includeHttps=1' \
      -H "X-Plex-Token: ${TOKEN}" \
      -H 'X-Plex-Client-Identifier: diag' \
      | jq '.[] | select(.provides=="server") | .connections[] | {local, port, uri}'
    ```

    Revealed two bogus `connections[]` entries being advertised:

    | Entry                                        | What Plex told plex.tv                          | Reachability                         |
    | -------------------------------------------- | ----------------------------------------------- | ------------------------------------ |
    | Pod-CIDR IP, port 32400, `local:true`        | auto-discovered from the container's NIC        | ❌ unreachable from LAN              |
    | External FQDN, port **32400**, `local:false` | Plex appended its default port to the HTTPS URL | ❌ gateway listens on 443, not 32400 |

### Root Cause

Two independent Plex-side URL-advertisement bugs:

1. **Plex auto-advertises every local interface IP** as `local:true`
   regardless of routability. In a Kubernetes deployment the container's
   pod-CIDR IP ends up in the list but no LAN client can route to it.
2. **If the HTTPS entry in `PLEX_ADVERTISE_URL` / `customConnections`
   lacks an explicit port**, Plex appends its own default (32400) when
   it publishes to plex.tv, producing `https://<fqdn>:32400`. With split-
   horizon DNS pointing that FQDN at the in-cluster ingress gateway,
   clients then hit the gateway on the wrong port and the SYN is silently
   dropped.

The app-level `location=unknown` on the Apple TV side is a secondary
effect: the client's connection-probing logic can't classify a URL it
never managed to connect to as either `lan` or `wan`, so it tags the
ensuing session request accordingly, which then triggers the transcoder's
internal-URL-construction fault on the server.

### Fix

**Pin `:443` on the advertised HTTPS URL** in the HelmRelease:

```yaml
env:
    PLEX_ADVERTISE_URL: "http://${SVC_PLEX_ADDR:-127.0.0.1}:32400,https://plex.${SECRET_DOMAIN:-local}:443"
```

Applied 2026-04-17. After Flux rolls the pod, re-run the
`/resources.json` query above and confirm the HTTPS entry's `port` is
`443` and `uri` ends `:443`.

**Pending: suppress pod-IP advertisement.** If Apple TV stalls still
occur after Fix 1, options in order of increasing blast radius:

1. Plex preference `advertiseIp=<LAN-LB-IP>` via an extra
   `PLEX_PREFERENCE_N` env var — forces Plex to publish only the given
   IP as its local address and skip interface auto-discovery.
2. `hostNetwork: true` on the Plex pod — Plex then only sees node IPs
   rather than the pod-CIDR IP. Has wider consequences (host port
   binding, LoadBalancer semantics, scheduling) so only reach for this
   if option 1 doesn't take.

### Verification

Re-query plex.tv and assert the published `connections[]` contains no
pod-CIDR IPs and no HTTPS entries on port 32400:

```bash
curl -s 'https://plex.tv/api/v2/resources.json?includeHttps=1' \
  -H "X-Plex-Token: ${TOKEN}" \
  -H 'X-Plex-Client-Identifier: diag' \
  | jq '.[] | select(.provides=="server") | .connections[]
         | select((.local==true and (.address|startswith("10.42.")))
                  or (.protocol=="https" and .port==32400 and .local==false))'
```

Should return nothing.

### Resolution Status

✅ **Fix 1 applied and stable** (2026-04-17, commit `f50e90c8`) — removes
the broken `https://<fqdn>:32400` entry. Zero `location=unknown` /
`expected MediaContainer element, found html` / `TranscodeUniversalRequest`
errors in `Plex Media Server.log` since the pod rolled.

In hindsight the BBR/MTU-probing fix (Option 1 above, commit `4a8086bb`)
was the dominant cause of user-visible flaps; this URL-advertisement fix
addressed a real but secondary server-side failure mode that coincided
with the same incident. Both fixes are now in place and Plex has been
stable since 2026-04-17.

**Fix 2 (pod-IP suppression) not applied** — still in reserve if Apple
TVs ever resume stalling on `10.42.*.*:32400` connection attempts.

### Adjacent observation

Plex's database occasionally stalls during scheduled library-analysis
bursts. Symptom: intra-cluster HTTP clients get 30s read timeouts hitting
the Plex Service, and the main log shows `WARN - MDE: unable to find a
working transcode profile` repeated at sub-second cadence alongside
`ERROR - Waited over 10 seconds for a busy database; giving up`. The pod
stays `Ready` because `/identity` is a trivial in-memory endpoint and
unaffected. If this becomes a user-visible problem, consider setting the
chapter thumbnail / BIF / voice-activity generators from `scheduled` to
`manual` in the HelmRelease preferences (`ButlerTaskDeepMediaAnalysis=0`
and `ScannerLowPriority=1` are already set).

---

## Talos Patch Rollout Gotchas (TUPPR v0.1.27 / Talos v1.13.0 → v1.13.2)

### Issue

A routine Talos patch upgrade (v1.13.0 → v1.13.2) driven by TUPPR
(`talosupgrade/talos` in `kubernetes/apps/system-upgrade/tuppr/`) repeatedly
got stuck mid-rollout. The Job's `talosctl upgrade` step completed cleanly
(installer ran, new UKI written, set as default boot entry), the drain
succeeded, but the **`talosctl reboot --mode=powercycle` step then failed
with an RPC timeout** to the cluster-internal `default/talos` Service IP.
The node was left:

- Cordoned (`SchedulingDisabled`)
- Still on the **old** Talos version (kernel + osImage unchanged)
- Drained, so 6 pods (mon-c, osd-X, dragonfly-N, postgres18-N, mosquitto-0,
  per-volume affinity) sat `Pending` indefinitely
- Ceph in `HEALTH_WARN` (1 mon + 2 OSDs down) for as long as the cordon stood

This blocked TUPPR's own health-check (`CephCluster.status.ceph.health ==
'HEALTH_OK'`) from passing, so TUPPR refused to create another upgrade Job.
Classic chicken-and-egg.

### Investigation (2026-05-13)

The Job pod log shows the install/drain succeed, then:

```text
cr-talos-XX: node drained
"10.32.8.YY": rpc error: code = Unavailable desc = connection error: desc =
  "transport: Error while dialing: dial tcp 10.43.106.194:50000: i/o timeout"
console logs for nodes ["10.32.8.YY"]:
```

`10.43.106.194:50000` is the `default/talos` Service ClusterIP, which proxies
to the per-node Talos API. The Service endpoint is hosted on a pod that was
itself evicted during the drain — so by the time TUPPR's wrapper tries to
issue the powercycle, the Service has no healthy endpoint, the RPC times out,
and the wrapper gives up. The node never gets the reboot command.

`bootID` confirms no reboot happened. `kubectl get nodes` shows
`LastTransitionTime` weeks-old (no Ready=False transition during the
attempt). `kernelVersion` and `osImage` stay on v1.13.0.

### Workarounds

#### Option A: Manual `talosctl reboot --mode=powercycle` (recommended)

The install is already staged — `sd-boot: using Talos-v1.13.X.efi as
default entry` is already in the META partition. A direct reboot from the
operator's host bypasses the broken in-cluster API path:

```bash
export TALOSCONFIG=./talos/clusterconfig/talosconfig
talosctl --nodes <node-ip> reboot --mode=powercycle
```

`talosctl` talks to the node directly (not through the cluster Service), so
it survives the drain that broke the in-cluster path. Node boots into the
new version, kubelet re-registers, Pending pods schedule back. Run
`kubectl uncordon <node>` afterwards — TUPPR's cordon does not auto-clear
when bypassed.

#### Option B: Break the deadlock with `kubectl uncordon`, then let TUPPR retry

If you'd rather TUPPR drive the rollout:

```bash
kubectl uncordon cr-talos-XX
```

Volume-pinned pods schedule back, Ceph recovers to `HEALTH_OK` within
~60-90 s, TUPPR's pre-flight health-check passes, TUPPR creates a fresh
upgrade Job. **Caveat**: the same RPC-timeout failure pattern often recurs
on the retry. For cr-talos-03 the recipe eventually worked after 1-2
retries; for cr-talos-02 it failed 3x in a row and we fell back to Option A.

### Related cleanup

#### Stale CSI VolumeAttachments after drain

After multiple drain/retry cycles on the same node, `VolumeAttachment`
records to the original node may persist with `ATTACHED=true` even though
no pod is using them. They block new attachments on the destination node
("Multi-Attach error" → "AttachVolume.Attach failed... volume attachment
is being deleted"). `kubectl delete volumeattachment <name>` hangs on the
`external-attacher/rook-ceph.rbd.csi.ceph.com` finalizer because the CSI
driver doesn't confirm detachment.

Force-clear the finalizer:

```bash
kubectl patch volumeattachment <csi-attachment-name> \
  --type=merge -p '{"metadata":{"finalizers":null}}'
```

Safe when the originating pod is already gone (the RBD kernel mapping is
cleaned up on pod termination; only the API-level record is stale).

#### KEDA `nfs-scaler` flap during the rollout

When the `blackbox-exporter-lan` pod gets shuffled by the drain, the
fresh pod's blackbox probe to `cr-storage-data:2049` returns
`probe_success=0` (with `probe_ip_addr_hash 0` — DNS resolve fails inside
the pod even though the same lookup works from `kubectl run --image=netshoot`
in the same namespace). This trips the
`components/nfs-scaler` KEDA `ScaledObject`, which scales NFS-dependent
deployments (notably Plex) to `0` even though NFS is genuinely reachable.

Restarting the blackbox-exporter pod usually clears it
(`kubectl delete pod -n observability -l app.kubernetes.io/name=prometheus-blackbox-exporter`).
If KEDA keeps flapping during recovery, pause it on the affected
ScaledObject with:

```bash
kubectl annotate scaledobject -n <ns> <name> \
  autoscaling.keda.sh/paused=true \
  autoscaling.keda.sh/paused-replicas=1 \
  --overwrite
```

Note: Flux may strip these annotations on the next HelmRelease reconcile
if they're not in the chart values — re-apply if KEDA scales the workload
back to 0.

### References

- TUPPR: <https://github.com/home-operations/tuppr>
- KEDA `paused-replicas` semantics:
  <https://keda.sh/docs/latest/concepts/scaling-deployments/#pause-autoscaling>
- editorconfig-checker is unrelated but documented for completeness: see
  `.github/linters/.editorconfig-checker.json` for the Markdown exclude.

### Resolution Status

⚠️ **Workarounds documented; root causes not yet fixed.** Open items:

1. TUPPR Job should not route its post-drain `reboot` call through a
   cluster-internal Service whose endpoint may have been evicted by the
   drain. Worth raising upstream or pinning `rebootMode: kexec` to skip
   the powercycle path.
2. The `blackbox-exporter-lan` pod's intermittent DNS-resolve failure on
   target hostnames needs investigation — likely a `dnsPolicy` /
   resolv.conf issue post-Cilium-identity-shuffle.
3. KEDA-on-Plex pause annotations get reverted by Flux; would be cleaner
   to set them through chart values so they survive reconcile.

---

**Last Updated**: 2026-05-13
**Cluster**: talos-cluster
