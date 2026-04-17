# Known Issues

This document tracks known issues and their workarounds in the cluster.

## 1Password Connect PushSecret False 400 Errors

### Issue

PushSecret resources generate spurious HTTP 400 errors in logs despite successfully syncing secrets to 1Password:

```
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

```
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
- BBR on LAN discussion: https://groups.google.com/g/bbr-dev (search "LAN burst")
- Talos sysctl defaults: `/etc/sysctl.d/`
- Cilium BandwidthManager: https://docs.cilium.io/en/stable/network/kubernetes/bandwidth-manager/

### Resolution Status

⏳ **Runtime workaround proven effective for MSS-collapse half of the problem**
(2026-04-17). BBR-disable half and permanent-fix options documented pending
rollout via Option 1 (Cilium egress annotation) or Option 2 (pod sysctls).

---

**Last Updated**: 2026-04-17
**Cluster**: talos-cluster
