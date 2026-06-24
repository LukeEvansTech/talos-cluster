# KB-018: Plex Remote 4K Transcode-Decision Crash (`bad lexical cast`)

**Status:** Understood; the fix is client-side or library-side, not a server change.

## Symptom

A remote user reports "Plex not working" on specific 4K titles while "it works on my phone".
The Plex server, GPU, and NFS are all healthy (pod up for days, 0 restarts). In
`Plex Media Server.log`, a burst of hundreds of identical triads:

```text
Bandwidth exceeded: 84800 kbps > 20000 kbps
Cannot make a decision because either the file is unplayable or the client provided bad data
Got exception from request handler: bad lexical cast: source type value could not be interpreted as target
Transcode runner appears to have died.
```

The client retries ~8×/sec (hundreds of errors in a minute or so) then gives up.

## Cause

A **remote** client (via the cloudflared tunnel; the session shows `location=wan`) sends a
decision request demanding a full transcode at **4K** (`videoResolution=4096x2160`,
`videoQuality=100`, `directPlay=0&directStream=0`) while the server clamps remote streams to
**20 Mbps** (`WanPerStreamMaxUploadRate=20000` = Settings → Network → Limit remote stream
bitrate). For ultra-high-bitrate 4K HDR HEVC remuxes (~42–49 Mbps average, ~85 Mbps peak,
lossless audio) Plex estimates ~84.8 Mbps needed for 4K, **refuses to downscale resolution**,
and **crashes the decision** instead of falling back.

**Why the phone works / why only these files:** LAN devices bypass the 20 Mbps WAN cap, and
remote clients that downscale to 1080p succeed under the *same* cap. Only the impossible
"4K at 20 Mbps" combination on the highest-peak files crashes.

## Fix

Keep the cap (it's intentional — sized so the home uplink supports ~5 concurrent remote
streams; one uncapped 4K direct-play would eat the whole link). Raising it doesn't even fix the
crash — only an effectively-unlimited cap would, which defeats the purpose. Fix it on the
client or in the library instead:

1. **Client-side (the fix):** on the failing device, set Remote/Internet Streaming Quality to
   a **1080p preset** (not Original/Maximum, which request 4K). The client then asks for
   `1920x1080` and transcodes ≤ 20 Mbps fine.
2. **Library-side:** produce **1080p versions** of the specific high-bitrate 4K remuxes (e.g.
   via tdarr) so even Maximum-quality clients direct-play the 1080p copy (~10–15 Mbps) with
   zero transcode; keep the 4K original for LAN.

The cap itself is declarative in git as `PLEX_PREFERENCE_14: "WanPerStreamMaxUploadRate=20000"`
in the Plex HelmRelease values (the home-operations image applies any
`PLEX_PREFERENCE_<n>="Key=Value"` env on start — no live API/UI change needed). There is **no**
server-side "limit remote resolution" setting in Plex, only bitrate.

## Recon notes

- Tautulli listens on `:80` (not the config's `8181`); its on-disk `config.ini` api_key can be
  stale versus the in-memory one — copy `/config/tautulli.db` out and query sqlite locally.
- The Plex token is in `Preferences.xml` (`PlexOnlineToken`); query the local API at
  `http://localhost:32400` inside the pod. Enable `logDebug=1` temporarily to capture the
  decision codes (`Direct Play=3000`, `Transcode=4005`).

## References

- Related Plex entries: [KB-002](002-plex-direct-play-buffering-bbr-mtu-probing.md),
  [KB-003](003-plex-advertises-broken-connection-urls.md).
