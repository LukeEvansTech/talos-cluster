# KB-003: Plex Advertises Broken Connection URLs To plex.tv

**Status:** Fix 1 applied and stable: removes the broken `https://<fqdn>:32400` entry. Zero `location=unknown` / `expected MediaContainer element, found html` / `TranscodeUniversalRequest` errors in `Plex Media Server.log` since the pod rolled. Fix 2 (pod-IP suppression) not applied: still in reserve if Apple TVs ever resume stalling on pod-CIDR `:32400` connection attempts.

## Symptom

Intermittent "server unavailable" / mid-stream reconnects on LAN clients (notably Apple TV) even though the Plex pod is healthy (0 restarts, 100% readiness, sub-ms `/identity` probe RTT from another node on the LAN). No pod restarts, no LB or HTTPRoute flaps, no node pressure.

Distinct from the BBR/MTU-probing buffering issue (see KB-002). This one manifests as connection drops and app-level "unavailable" states, typically at session start or at session re-negotiation boundaries rather than mid-stream 60-second freezes.

## Cause

Captured in parallel during a reported flap:

- LAN-side `curl /identity` probe from a hostNetwork pod on a non-pod-host node: **zero failures**, sub-ms response, for the entire window.
- `talosctl pcap` on both the pod host and the L2 announcer node showed the Apple TV client opening TCP sockets to **two IPs simultaneously**: the Plex LoadBalancer IP (healthy) **and** the gateway LoadBalancer IP on port 32400 (repeated SYN retransmits, no SYN+ACK: the gateway doesn't listen on 32400, only 443).
- Apple TV pcap showed `RST` bursts against the _healthy_ Plex socket exactly at the moment the user reported the drop. Not caused by server or network: client-side session teardown.
- Plex's own `Plex Media Server.log` at the drop timestamp:

    ```text
    ERROR - [Req#…/Transcode] Invalid value for 'location' client location attribute: unknown
    ERROR - [Req#…/Transcode] downloadContainer: expected MediaContainer element, found html
    ERROR - [Req#…/Transcode] TranscodeUniversalRequest: unable to get container: ///library/metadata/<id>?…
    ```

    Three tells in that trio: `location … unknown` (the client's request URL didn't match any connection it considered LAN or WAN); HTML body where the transcoder expected XML (an internal fetch hit a gateway error page); and the triple slash `///library/metadata/…`, a classic "empty host in URL-join" bug inside Plex's internal transcoder.

- Querying plex.tv for what the server is actually publishing:

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
    | Pod-CIDR IP, port 32400, `local:true`        | auto-discovered from the container's NIC        | unreachable from LAN                 |
    | External FQDN, port **32400**, `local:false` | Plex appended its default port to the HTTPS URL | gateway listens on 443, not 32400    |

Root cause is two independent Plex-side URL-advertisement bugs:

1. **Plex auto-advertises every local interface IP** as `local:true` regardless of routability. In a Kubernetes deployment the container's pod-CIDR IP ends up in the list but no LAN client can route to it.
2. **If the HTTPS entry in `PLEX_ADVERTISE_URL` / `customConnections` lacks an explicit port**, Plex appends its own default (32400) when it publishes to plex.tv, producing `https://<fqdn>:32400`. With split-horizon DNS pointing that FQDN at the in-cluster ingress gateway, clients then hit the gateway on the wrong port and the SYN is silently dropped.

The app-level `location=unknown` on the Apple TV side is a secondary effect: the client's connection-probing logic can't classify a URL it never managed to connect to as either `lan` or `wan`, so it tags the ensuing session request accordingly, which then triggers the transcoder's internal-URL-construction fault on the server.

## Fix

**Pin `:443` on the advertised HTTPS URL** in the HelmRelease:

```yaml
env:
  PLEX_ADVERTISE_URL: "http://${SVC_PLEX_ADDR:-127.0.0.1}:32400,https://plex.${SECRET_DOMAIN:-local}:443"
```

After Flux rolls the pod, re-run the `/resources.json` query above and confirm the HTTPS entry's `port` is `443` and `uri` ends `:443`.

### Pending: suppress pod-IP advertisement

If Apple TV stalls still occur after Fix 1, options in order of increasing blast radius:

1. Plex preference `advertiseIp=<LAN-LB-IP>` via an extra `PLEX_PREFERENCE_N` env var: forces Plex to publish only the given IP as its local address and skip interface auto-discovery.
2. `hostNetwork: true` on the Plex pod: Plex then only sees node IPs rather than the pod-CIDR IP. Has wider consequences (host port binding, LoadBalancer semantics, scheduling) so only reach for this if option 1 doesn't take.

### Verification

Re-query plex.tv and assert the published `connections[]` contains no pod-CIDR IPs and no HTTPS entries on port 32400:

```bash
curl -s 'https://plex.tv/api/v2/resources.json?includeHttps=1' \
  -H "X-Plex-Token: ${TOKEN}" \
  -H 'X-Plex-Client-Identifier: diag' \
  | jq '.[] | select(.provides=="server") | .connections[]
         | select((.local==true and (.address|startswith("<pod-cidr-prefix>")))
                  or (.protocol=="https" and .port==32400 and .local==false))'
```

Should return nothing.

## Adjacent observation

Plex's database occasionally stalls during scheduled library-analysis bursts. Symptom: intra-cluster HTTP clients get 30s read timeouts hitting the Plex Service, and the main log shows `WARN - MDE: unable to find a working transcode profile` repeated at sub-second cadence alongside `ERROR - Waited over 10 seconds for a busy database; giving up`. The pod stays `Ready` because `/identity` is a trivial in-memory endpoint and unaffected. If this becomes a user-visible problem, consider setting the chapter thumbnail / BIF / voice-activity generators from `scheduled` to `manual` in the HelmRelease preferences (`ButlerTaskDeepMediaAnalysis=0` and `ScannerLowPriority=1` are already set).
