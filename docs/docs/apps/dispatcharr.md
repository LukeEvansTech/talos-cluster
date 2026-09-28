# Dispatcharr

IPTV playlist/EPG manager in the `media` namespace, routed through a gluetun VPN sidecar. The
sidecar's shape (native sidecar ordering, kubelet probes, the memory-limit exception) is copied
from `downloads/prowlarr`; this page covers what is specific to dispatcharr.

## Why this pod's DNS goes out the tunnel

`downloads/prowlarr`, `downloads/qbittorrent` and `downloads/sabnzbd` all set
`DNS_KEEP_NAMESERVER: "on"`, leaving CoreDNS as the resolver and gluetun's own DoT resolver unused.
Dispatcharr is the one gluetun sidecar in this repository that sets it `off`: gluetun owns
`resolv.conf` here, and every lookup this pod makes goes out gluetun's own DoT resolver, inside the
WireGuard tunnel.

The reason is specific to the IPTV panel this app talks to. Estate DNS forwards to NextDNS, whose
`ddns` rule sinkholes dynamic-DNS hostnames to `0.0.0.0`, and the panel 302s every stream to a
rotating No-IP hostname (six such domains in use at one count, the set moving over time).
Allowlisting them one at a time is a moving target, and the failure mode when a new one hits the
sinkhole is a played-fine, authed-fine, spinner-forever player, which reads exactly like a dead
provider rather than a DNS block. Taking this one pod off estate DNS resolves the whole class of
hostname permanently. `downloads/imgur-proxy` also bypasses estate DNS, by a different mechanism
(its nginx config, not `DNS_KEEP_NAMESERVER`; see
[hardening backlog H-21](../operations/hardening-backlog.md#h-21-a-comment-claimed-nextdns-is-enforced-on-every-pod-except-one-which-is-not-true)),
so this is not the only exception to estate DNS in the fleet. The OPNsense `:53` redirect doesn't
see this pod's traffic at all: it matches plaintext port 53 to public destinations, and this is DoT
(853) carried inside WireGuard.

The cost is that `.cluster.local` no longer resolves inside this pod. Dragonfly is the one
in-cluster dependency, and it's pinned via `hostAliases` in `app/helmrelease.yaml` instead.

## Redis: isolated from prowler's queue

Dispatcharr uses its own Dragonfly logical database (`REDIS_DB: "3"`), separate from prowler's
worker on db 0. Before this, prowler's worker was consuming and discarding dispatcharr's EPG/M3U
Celery tasks: dispatcharr's uwsgi process spawns its Celery workers through `attach-daemon`, and
that child process does not inherit `CELERY_BROKER_URL` from the parent's environment, so it always
falls back to the URL it builds itself from `REDIS_*`. That fallback URL is what `REDIS_DB`
actually controls; setting `CELERY_BROKER_URL` in the `ExternalSecret` template keeps both code
paths pointed at the same database, but by itself does not move the workers off db 0. The fix was
confirmed by connection state (`CLIENT LIST` showing the workers on `db=3`), not by reading the
environment back, since an unused env var would look identical either way.

## One VPN device key per pod

`gluetun-dispatcharr` is its own 1Password item with its own VPN-provider device key, not shared
with any other gluetun sidecar. The VPN provider's device keys are single-connection: two pods
authenticating with the same key at once flap both tunnels.

## Worth checking: `BLOCK_MALICIOUS`

`BLOCK_MALICIOUS` is off, with a comment pointing at
[qdm12/gluetun#2054](https://github.com/qdm12/gluetun/issues/2054) (skipping gluetun's roughly
300MB malicious-domain blocklist). The comment this was trimmed from also claimed gluetun's own
resolver is "never in the path" for this pod, which was true when `downloads/prowlarr` had the
identical setting, but prowlarr sets `DNS_KEEP_NAMESERVER: "on"` while dispatcharr sets it `off`.
With gluetun's DoT resolver actually handling every lookup here (see above), that specific
justification for leaving malicious-domain blocking off does not carry over, and this may be worth
a second look independent of this comment sweep.
