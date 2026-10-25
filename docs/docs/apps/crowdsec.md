# CrowdSec (alert-only)

CrowdSec reads the `envoy-external` access log, which carries every request that arrives through
the Cloudflare tunnel, and flags source addresses that behave like scanners, brute-forcers or bad
bots. It runs in **alert-only** mode: there is no bouncer, so a decision is recorded and nothing is
blocked.

## How it is wired

- The **agent** (`crowdsec-agent`) streams the gateway pods' logs through the Kubernetes API
  (its Role grants `pods/log` in `network` only) and parses them with the `yanis-kouidri/envoy`
  collection.
- The **local API** (`crowdsec-lapi`) stores alerts and decisions on two small `ceph-block` PVCs,
  and pulls the community blocklist.
- The parser takes the visitor from `downstream_remote_address`. That is the real client address,
  not the cloudflared pod, because the envoy `ClientTrafficPolicy` trusts `X-Forwarded-For` from
  the pod network. If that trust is removed, every request is attributed to the cloudflared pod,
  and the bundled `crowdsecurity/whitelists` parser drops private-address events, so detection
  stops silently: an empty alert list then means blind, not quiet.
- It is separate from the CrowdSec instance on the firewall. The firewall cannot block tunnel
  visitors anyway: their traffic reaches the cluster from Cloudflare, not from their own address.

## What it would have banned

```bash
kubectl -n network exec deploy/crowdsec-lapi -- cscli alerts list
kubectl -n network exec deploy/crowdsec-lapi -- cscli decisions list --origin crowdsec
kubectl -n network exec deploy/crowdsec-agent -- cscli metrics
```

`--origin crowdsec` shows only this cluster's own decisions. The community blocklist is hidden
by default; `cscli decisions list --all` includes it. Check each flagged address against the people who use the public apps
(family on mobile data, remote Plex and Jellyfin users) before turning enforcement on.

## Turning enforcement on

Add the bouncer: a `crowdsec-envoy-bouncer` release registered with the local API, and an Envoy
Gateway `SecurityPolicy` on `envoy-external` that calls it as an external authoriser.
onedr0p's `kubernetes/apps/network/crowdsec/bouncer` in `home-ops` is the reference. Allowlist your
own addresses first (a `cscli allowlists` entry), and alert on the bouncer's health, since a
failing external authoriser can reject every request.

## Console

The local API enrols in the CrowdSec console as `talos-cluster`, with `ENROLL_KEY` from the
`crowdsec` item in the `Talos` vault. A new enrolment shows in the console as pending; it sends
alerts only once it is accepted there.
