# KB-026: Plex Apple TV App Freezes on One Frame (Client Receive-Window Deadlock)

**Status:** Understood; **server-side clean: this is a Plex-for-Apple-TV client bug**, isolated by an Infuse control (same device/network/server/file plays fine in Infuse). Root cause narrowed same-day to **EAC3 direct play on tvOS 26.5** (see Cause); community-corroborated, no fixed app version as of 2026-07-12. A server-side `Profiles/tvOS.xml` override (transcode EAC3 audio → ac3 for tvOS clients only) is deployed as a workaround: see Fix.

## Symptom

A LAN Apple TV shows **one still frame of a 4K title, then the Plex app freezes**. The app becomes unresponsive and you must **force-quit and re-open it** (backing out / pressing play again does nothing). Intermittent: the *same* title sometimes plays for minutes, sometimes freezes at 0s. Affects both **Dolby Vision** (e.g. Silo S3 `[DV HDR10Plus]`) and plain **HDR10** 4K titles (e.g. *500 Days of Summer* `4K HDR10`, `DOVIPresent=0`); low-bitrate/1080p content (e.g. a 3 Mbps Pokémon film) is unaffected.

Distinct from the other Plex Apple TV entries:

- Not KB-002 (BBR/MTU): that's periodic mid-stream freezes that **auto-recover** after ~60s; this one never recovers and starts at 0s.
- Not KB-003 (broken connection URLs): none of its tells (`location=unknown`, `found html`, `///library`) appear in the server log.
- Not KB-018 (remote 4K decision crash): the client is `local=1 location=lan`, direct-play, no transcode.

## Cause

**A bug in the Plex for Apple TV client** (observed on app **8.45** / **tvOS 26.5**, `AppleTV11,1`). The app's player stalls at playback start, stops draining its TCP socket, and the connection deadlocks in **zero-window persist**. The server is healthy and holds the next segment ready. It simply can't send because the client's receive window is shut.

**The isolating control:** playing the *same file from the same Plex server on the same Apple TV via **Infuse*** works flawlessly. Same hardware, same tvOS, same TV/receiver + HDR handshake, same network path, same server egress (Cilium BBR + `200M` cap apply to Infuse too), same Dolby-Vision content. The **only** changed variable is the player app. That eliminates the cluster, the network, the A12 hardware, the display/HDR handshake, and the content, leaving the Plex app itself.

**Server-observable signature**: captured with `ss -tnie` inside the Plex pod netns during a live freeze, on the video socket to the Apple TV (`10.42.1.109:32400 → <apple-tv-ip>:5xxxx`):

```text
ESTAB  Send-Q=3899264  timer:(persist,1min13sec,0) backoff:9
  bbr mss:1448 pmtu:1500 cwnd:346 ssthresh:140
  bytes_sent:157091065 bytes_acked:157090033 retrans:0/1   <-- client ACKed ~157 MB, ~zero loss
  pacing_rate 1029Mbps delivery_rate 47Mbps
  rwnd_limited:146880ms(95.7%) notsent:3899264             <-- blocked 95.7% by the receiver's window
```

Read it as: the Apple TV **received ~157 MB of video and still had `viewOffset=0`** (nothing played), then drove its receive window to zero and left it there. The sender parked 3.9 MB in `notsent` and entered persist mode; the persist timer backed off nine times (`backoff:9`), so even if the client reopened the window it would take minutes to resume → permanent freeze. Because the client already holds tens of seconds of buffered video, this is **not** starvation, bandwidth, loss, or decode horsepower. It's the app **refusing to start playback** and consequently stopping its reads.

Why it isn't universal ("wouldn't everyone see this?"): a 24 Mbps 4K HEVC stream is trivial for an A12. Capability is universal, so a hardware limit would be a famous complaint and isn't. A **startup/player bug in one client build on a brand-new tvOS** is version-specific: only users on that Plex-app + tvOS combo hit it; many are on other versions or use Infuse.

### Narrowed root cause: EAC3 direct play on tvOS 26.5

Re-examining the test matrix, the discriminating variable is the **audio codec**, not resolution or HDR format:

| Title | Video | Audio | Plex tvOS app |
| --- | --- | --- | --- |
| Silo S3E1/E2 | 4K HEVC DV P8.1 | **EAC3** Atmos 5.1 | freezes |
| 500 Days of Summer | 4K HEVC HDR10 (no DV) | **EAC3** 5.1 | freezes |
| Pokémon: The First Movie | 1080p HEVC SDR | **Opus** 2.0 | plays fine |
| any of the above via Infuse | — | EAC3 | plays fine (own decoder, not the tvOS pipeline) |

Community reports match exactly: video freezes on a frame during **EAC3 direct play on tvOS 26.5**, with the same files fine on **tvOS 26.4** and on other platforms
(<https://forums.plex.tv/t/eac3-audio-apple-tv-direct-play-broken-video-freezes-audio-continues-tvos-26-5-synology-ds152/938778>). Plex staff engaged but could not
reproduce universally; no fixed app version identified as of 2026-07-12 (tvOS app releases moved to year-based versions, e.g. 2026.13.0 on 2026-06-30, no EAC3 fix in
its notes). Even Plex's bundled `tvOS.xml` profile carries a comment admitting historical tvOS EAC3 quirks ("Since tvOS may have issues direct playing mov/eac3…").

## Fix

The bug is in the client, but with the cause narrowed to EAC3 there is a **surgical server-side workaround** plus client-side remediations:

1. **Server-side workaround (deployed):** a custom `Profiles/tvOS.xml` override (`kubernetes/apps/media/plex/app/resources/tvOS.xml`, shipped as the `plex-client-profiles` ConfigMap and mounted over the config dir's `Profiles/`) mirrors the Apple TV 4K's real capabilities but **removes `eac3` from every audioCodec list**. EAC3 titles then direct-stream with an **ac3 5.1 audio transcode** (surround preserved, Atmos metadata lost, tvOS clients only); video stays a copy (`hevc` is in the HLS transcode target). Delete the file + mount to restore stock behaviour once Plex ships a fix. Caveat: the app's `X-Plex-Client-Profile-Extra` deltas may re-add capabilities client-side. Verify with `/status/sessions` (`videoDecision=copy`, `audioDecision=transcode`) and revert if the decision doesn't change.
2. **Client:** update the Plex app past 8.45 (App Store; year-versioned builds like 2026.13.x are newer) and fully power-cycle the Apple TV. If freezes persist, toggle the app's **updated audio engine** setting. It switches the exact EAC3 path that broke.
3. **Fallback:** play via **Infuse**: its own decoder pipeline is unaffected and keeps full Atmos.

The server-side TCP levers (lower the `200M` egress cap, force-disable BBR via `allowed-unsafe-sysctls`) are **not** indicated here: the capture shows `retrans:0/1` (no loss) and a receiver-limited window, so the wire and congestion control are not the bottleneck.

## Recon notes

- **Detect the freeze objectively:** poll `/status/sessions` and watch `viewOffset`: a frozen client keeps reporting `state=playing` while `viewOffset` stops advancing (stuck at `0s` here). Don't trust `state` alone.
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

- **Source-IP is SNAT'd.** The Plex Service is `externalTrafficPolicy: Cluster`, so a filter like `ss dst <apple-tv-ip>` finds nothing. Client media sockets can appear with node/masquerade peers, though here the Apple TV's real IP `<apple-tv-ip>` did show on the video socket. Filter by `sport = :32400` and look for the socket with a large `Send-Q` / `persist` timer.
- **Confirm the egress cap is really enforced** (not just annotated): `cilium-dbg bpf bandwidth list` on the node's agent. The Plex endpoint id (from `cilium-dbg endpoint list` matching `10.42.1.109`) should show `Egress … 200M`. It was correctly enforced here (endpoint `1001 → 200M`), which is why this is *not* a KB-002 relapse.
- **Dead-ends ruled out this run (so nobody re-chases them):**
    - *GPU/libcuda:* the driver moved `libcuda.so.1` from `/usr/local/glibc/usr/lib` (the KB-era `LD_LIBRARY_PATH`) to `/usr/local/lib` after the 570→595 bump, but **musl already searches `/usr/local/lib`**, so `LD_PRELOAD=libcuda.so.1` loads fine on either path. The stale `LD_LIBRARY_PATH` is harmless, not a bug.
    - *RPU `hevc … RPU validation failed`:* a single unrelated remote transcode session (`iqi9…`), ~136k lines over Jul 11 16:28-18:03, **zero since**; not the Apple TV, not this symptom.

## References

- Related Plex entries: [KB-002](002-plex-direct-play-buffering-bbr-mtu-probing.md), [KB-003](003-plex-advertises-broken-connection-urls.md), [KB-018](018-plex-remote-4k-transcode-decision-crash.md).
- Linux TCP zero-window / persist timer: `net/ipv4/tcp_output.c` (`tcp_send_probe0`, exponential `icsk_backoff`).
- EAC3 / tvOS 26.5 direct-play breakage (matching report): <https://forums.plex.tv/t/eac3-audio-apple-tv-direct-play-broken-video-freezes-audio-continues-tvos-26-5-synology-ds152/938778>
- Broader tvOS 26 Plex app instability: <https://forums.plex.tv/t/plex-app-crashing-on-all-my-apple-tvs/929051>, <https://forums.plex.tv/t/cannot-play-any-videos-on-apple-tv-plex-app-tvos-26-3-23k620/937417>
- Custom tvOS client-profile override precedent: <https://gist.github.com/jfeilbach/18b08ea0ed9eaf844d643ab092905973>
