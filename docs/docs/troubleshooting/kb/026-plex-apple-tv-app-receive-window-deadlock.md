# KB-026: Plex Apple TV App Freezes on One Frame (Client Receive-Window Deadlock)

**Status:** Understood; **server-side clean — this is a Plex-for-Apple-TV client bug**, isolated by an Infuse control (same device/network/server/file plays fine in Infuse). No cluster change fixes it; remediation is client-side (use Infuse, or reinstall/downgrade the Plex tvOS app).

## Symptom

A LAN Apple TV shows **one still frame of a 4K title, then the Plex app freezes** — the app becomes unresponsive and you must **force-quit and re-open it** (backing out / pressing play again does nothing). Intermittent: the *same* title sometimes plays for minutes, sometimes freezes at 0s. Affects both **Dolby Vision** (e.g. Silo S3 `[DV HDR10Plus]`) and plain **HDR10** 4K titles (e.g. *500 Days of Summer* `4K HDR10`, `DOVIPresent=0`); low-bitrate/1080p content (e.g. a 3 Mbps Pokémon film) is unaffected.

Distinct from the other Plex Apple TV entries:

- Not KB-002 (BBR/MTU) — that's periodic mid-stream freezes that **auto-recover** after ~60s; this one never recovers and starts at 0s.
- Not KB-003 (broken connection URLs) — none of its tells (`location=unknown`, `found html`, `///library`) appear in the server log.
- Not KB-018 (remote 4K decision crash) — the client is `local=1 location=lan`, direct-play, no transcode.

## Cause

**A bug in the Plex for Apple TV client** (observed on app **8.45** / **tvOS 26.5**, `AppleTV11,1`). The app's player stalls at playback start, stops draining its TCP socket, and the connection deadlocks in **zero-window persist**. The server is healthy and holds the next segment ready — it simply can't send because the client's receive window is shut.

**The isolating control:** playing the *same file from the same Plex server on the same Apple TV via **Infuse*** works flawlessly. Same hardware, same tvOS, same TV/receiver + HDR handshake, same network path, same server egress (Cilium BBR + `200M` cap apply to Infuse too), same Dolby-Vision content — the **only** changed variable is the player app. That eliminates the cluster, the network, the A12 hardware, the display/HDR handshake, and the content, leaving the Plex app itself.

**Server-observable signature** — captured with `ss -tnie` inside the Plex pod netns during a live freeze, on the video socket to the Apple TV (`10.42.1.109:32400 → <apple-tv-ip>:5xxxx`):

```text
ESTAB  Send-Q=3899264  timer:(persist,1min13sec,0) backoff:9
  bbr mss:1448 pmtu:1500 cwnd:346 ssthresh:140
  bytes_sent:157091065 bytes_acked:157090033 retrans:0/1   <-- client ACKed ~157 MB, ~zero loss
  pacing_rate 1029Mbps delivery_rate 47Mbps
  rwnd_limited:146880ms(95.7%) notsent:3899264             <-- blocked 95.7% by the receiver's window
```

Read it as: the Apple TV **received ~157 MB of video and still had `viewOffset=0`** (nothing played), then drove its receive window to zero and left it there. The sender parked 3.9 MB in `notsent` and entered persist mode; the persist timer backed off nine times (`backoff:9`), so even if the client reopened the window it would take minutes to resume → permanent freeze. Because the client already holds tens of seconds of buffered video, this is **not** starvation, bandwidth, loss, or decode horsepower — it's the app **refusing to start playback** and consequently stopping its reads.

Why it isn't universal ("wouldn't everyone see this?"): a 24 Mbps 4K HEVC stream is trivial for an A12 — capability is universal, so a hardware limit would be a famous complaint and isn't. A **startup/player bug in one client build on a brand-new tvOS** is version-specific: only users on that Plex-app + tvOS combo hit it; many are on other versions or use Infuse.

## Fix

Nothing to change in the cluster. Remediate on the client:

1. **Immediate workaround:** play via **Infuse** (it reads straight from the Plex library and its player doesn't have the bug).
2. **Plex app:** delete + reinstall the Plex app on the Apple TV (clears player state/cache); update past 8.45 if a newer build exists. tvOS can't easily downgrade an App Store app, so if the newest build is affected, sit it out / file a Plex report.
3. Optional: toggle the Plex app's Direct Play / Direct Stream and buffering options — a streaming-mode change occasionally sidesteps the stall.

The server-side TCP levers (lower the `200M` egress cap, force-disable BBR via `allowed-unsafe-sysctls`) are **not** indicated here — the capture shows `retrans:0/1` (no loss) and a receiver-limited window, so the wire and congestion control are not the bottleneck.

## Recon notes

- **Detect the freeze objectively:** poll `/status/sessions` and watch `viewOffset` — a frozen client keeps reporting `state=playing` while `viewOffset` stops advancing (stuck at `0s` here). Don't trust `state` alone.
- **Get into the right netns.** `pgrep -f "Plex Media Server" | head -1` on a `hostPID` debug pod can match a **host-netns** helper (you'll see the node IP `<node-ip>` / `cilium_host` instead of `10.42.1.109`). Iterate all matches and pick the PID whose `nsenter -t $P -n ip -4 -o addr` actually contains the pod IP `10.42.1.109`:

    ```bash
    kubectl apply -f - <<'EOF'   # privileged, hostPID/hostNetwork, on the Plex node
    apiVersion: v1
    kind: Pod
    metadata: {name: plex-netdiag, namespace: media}
    spec:
      nodeName: <node>
      hostPID: true
      hostNetwork: true
      restartPolicy: Never
      tolerations: [{operator: Exists}]
      containers:
        - {name: net, image: nicolaka/netshoot:latest, command: ["sleep","900"], securityContext: {privileged: true}}
    EOF
    kubectl exec -n media plex-netdiag -- sh -c '
      for x in $(pgrep -f "Plex Media Server"); do
        nsenter -t $x -n ip -4 -o addr show | grep -q 10.42.1.109 && P=$x && break; done
      nsenter -t $P -n ss -tnie "( sport = :32400 )"'
    ```

- **Source-IP is SNAT'd.** The Plex Service is `externalTrafficPolicy: Cluster`, so a filter like `ss dst <apple-tv-ip>` finds nothing — client media sockets can appear with node/masquerade peers, though here the Apple TV's real IP `<apple-tv-ip>` did show on the video socket. Filter by `sport = :32400` and look for the socket with a large `Send-Q` / `persist` timer.
- **Confirm the egress cap is really enforced** (not just annotated): `cilium-dbg bpf bandwidth list` on the node's agent — the Plex endpoint id (from `cilium-dbg endpoint list` matching `10.42.1.109`) should show `Egress … 200M`. It was correctly enforced here (endpoint `1001 → 200M`), which is why this is *not* a KB-002 relapse.
- **Dead-ends ruled out this run (so nobody re-chases them):**
    - *GPU/libcuda:* the driver moved `libcuda.so.1` from `/usr/local/glibc/usr/lib` (the KB-era `LD_LIBRARY_PATH`) to `/usr/local/lib` after the 570→595 bump, but **musl already searches `/usr/local/lib`**, so `LD_PRELOAD=libcuda.so.1` loads fine on either path — the stale `LD_LIBRARY_PATH` is harmless, not a bug.
    - *RPU `hevc … RPU validation failed`:* a single unrelated remote transcode session (`iqi9…`), ~136k lines over Jul 11 16:28–18:03, **zero since**; not the Apple TV, not this symptom.

## References

- Related Plex entries: [KB-002](002-plex-direct-play-buffering-bbr-mtu-probing.md), [KB-003](003-plex-advertises-broken-connection-urls.md), [KB-018](018-plex-remote-4k-transcode-decision-crash.md).
- Linux TCP zero-window / persist timer: `net/ipv4/tcp_output.c` (`tcp_send_probe0`, exponential `icsk_backoff`).
