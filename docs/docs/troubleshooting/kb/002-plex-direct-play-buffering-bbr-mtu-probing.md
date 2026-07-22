# KB-002: Plex Direct-Play Buffering on LAN Apple TVs (BBR + MTU Probing)

**Status:** Permanent fix applied via Option 1 (Cilium egress annotation, `kubernetes.io/egress-bandwidth: "200M"`). Plex pod stable since: no restarts, no user-reported flaps, no MSS-collapse signal. Option 2 (pod sysctls via `allowed-unsafe-sysctls`) kept documented as a fallback if Cilium's BandwidthManager is ever disabled.

## Symptom

4K direct-play streams from Plex to Apple TVs on the wired LAN experience a ~60-second buffering pause every ~6 minutes. The pattern is independent of client (affects both Apple TVs), independent of Wi-Fi vs wired, and does not affect concurrent remote-WAN transcoded sessions.

From the player's perspective: playback freezes, the client buffer drains, then playback resumes once a new TCP connection has refilled the client buffer.

Distinct from the connection-URL advertisement issue (see KB-003). This one manifests as periodic mid-stream freezes rather than app-level "unavailable" states.

## Cause

Captured via `ss -tni` on the Plex pod, from a privileged `kubectl debug` ephemeral container (netshoot + `netadmin` profile). The investigation ruled out the NFS media read path (sustains 1.5 GB/s), Rook-Ceph (`HEALTH_OK`, pgs clean), the node 25G NIC (no errors), the Cilium data plane (no drops), GPU contention with Ollama, and the switch/LAG path, all clean under load.

The failure sequence on the long-lived video socket:

1. Every ~6 minutes a new TCP video socket opens (`src=:32400 dst=<node-ip>:49xxx`, `rcvmss:1056`, the HLS 4K-segment signature) and starts direct-playing.
2. The Apple TV Plex client drives its receive window down toward zero (`snd_wnd` from `~130000` to `64` to `32`) and enters **persist mode**.
3. BBR paces at LAN-speed bandwidth estimates (~1 Gbps `pacing_rate` while actual `delivery_rate` is 6-10 Mbps). It bursts into the tiny window and experiences loss.
4. The retransmit/timeout bursts trip `tcp_mtu_probing=1` black-hole detection, which collapses the connection's MSS (`1448 → 758 → 128 → 64`).
5. Once MSS is tiny and `cwnd:1`, RTO backs off to 100+ seconds and the connection is effectively dead even though `ESTABLISHED`.
6. Apple TV silently opens a new 49xxx socket → playback resumes → cycle repeats at the 6-minute mark.

Key `ss -tni` fields from a dying socket (live evidence):

```text
ESTAB 0 3034256 :32400 <node-ip>:49359
  bbr ... mss:64 pmtu:1500 cwnd:1 ssthresh:158
  bytes_sent:497577854 bytes_retrans:2380056
  retrans:1/1829 lost:133 rehash:9
  rto:104448 backoff:9
  rwnd_limited:571468ms(99.6%)
  pacing_rate:1040908216bps delivery_rate:27876584bps
  snd_wnd:189248 rcv_wnd:68608
```

Parallel control-channel sockets to the same Apple TV IP stayed healthy (`mss:1448`, `cwnd:250-320`, zero retrans), confirming this is workload-specific to the long-lived high-throughput video socket, not a path-level problem.

Root cause is a two-part interaction:

1. **Plex explicitly sets BBR per-socket** via `setsockopt(TCP_CONGESTION, "bbr")`, overriding any netns-default congestion control.
2. **BBR on sub-ms LAN RTT** estimates enormous bandwidth (mrtt ~0.15ms, bw ~1Gbps). When the receiver (tvOS Plex client) closes its receive window down to trickle levels, BBR's next probe after the window re-opens bursts at ~1 Gbps into a receiver that cannot consume it. Losses occur at the Apple TV's receive ring buffer / socket buffer.
3. **`tcp_mtu_probing=1`** (Talos default) reacts to the burst losses by halving the MSS, then halving again: the classic TCP black-hole detection false-positive. Once MSS < 100, throughput collapses.

## Fix

### Permanent fix Option 1 (recommended): Cilium pod-egress bandwidth annotation

Caps the pod's egress at the BPF EDT layer, which neutralizes BBR's LAN burst pacing without touching TCP config. Treats the symptom at exactly the right layer. Add to `kubernetes/apps/media/plex/app/helmrelease.yaml`:

```yaml
spec:
  values:
    controllers:
      plex:
        annotations:
          # existing annotations (e.g. reloader.stakater.com/auto: "true")
        pod:
          annotations:
            kubernetes.io/egress-bandwidth: "200M"
```

Requires Cilium's `bandwidth-manager`, which is already enabled in this cluster (`BandwidthManager: EDT with BPF [BBR]` per `cilium status`).

### Permanent fix Option 2: Pod-level safeSysctls

Set the two sysctls at pod spec level so they survive restarts. Requires adding these to the kubelet's `allowed-unsafe-sysctls` (Talos machine config `.machine.kubelet.extraArgs`), more invasive.

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

### Runtime, ephemeral workaround

Applied via `nsenter` into the Plex pod's netns from a privileged node-debug pod. Requires `NET_ADMIN` in the target netns, not node-wide. Lost on pod restart:

```bash
# Find Plex PID on the node running the Plex pod
kubectl debug node/<node> -it --image=nicolaka/netshoot:latest --profile=sysadmin
# Inside the debug pod:
PLEX_PID=$(pgrep -f "/plexmediaserver/Plex Media Server" | head -1)

# Fix 1: stop MSS collapse (confirmed: MSS held at 1448)
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_mtu_probing=0

# Fix 2: disallow BBR so Plex's setsockopt falls back to netns default
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_allowed_congestion_control="cubic reno"
nsenter -t $PLEX_PID -n sysctl -w net.ipv4.tcp_congestion_control=cubic
```

### Verification

After applying, confirm no video socket MSS-collapses during a 15-minute direct-play session:

```bash
# From a debug container in the Plex pod:
watch -n 10 "ss -tnoi 'dst <apple-tv-ip>' | grep -B1 'rcvmss:1056'"
```

Healthy signal: `mss` stays at `1448`, `cwnd` stays above ~50, `snd_wnd` fluctuates but `retrans` count does not spike.

## References

- Linux TCP persist timer / zero-window probing: `net/ipv4/tcp_output.c`
- BBR on LAN discussion: <https://groups.google.com/g/bbr-dev> (search "LAN burst")
- Talos sysctl defaults: `/etc/sysctl.d/`
- Cilium BandwidthManager: <https://docs.cilium.io/en/stable/network/kubernetes/bandwidth-manager/>
