# Imgur proxy

`kubernetes/apps/downloads/imgur-proxy` is an in-cluster SNI relay that geo-unblocks `imgur.com`
and its subdomains, reached through a `gluetun` WireGuard sidecar to the VPN provider (see
[split-dns](../architecture/split-dns.md) for how OPNsense redirects the zone to it). This page
keeps the incident detail behind the sidecar's hardening, which does not fit as manifest comments.

## The gluetun sidecar drops every capability

Every other gluetun sidecar in `downloads` (qbittorrent, sabnzbd, prowlarr) keeps root's default
capability set. This one sets `capabilities.drop: [ALL]`, which surfaced two upstream behaviours
that the other three silently tolerate:

- `cmd/gluetun/main.go` creates its status directory with `os.MkdirAll("/tmp/gluetun", 0644)`, a
  directory with no execute bit, so traversing into it needs `CAP_DAC_OVERRIDE`. The container pre-
  creates the path itself as an `emptyDir` (mode `1777`), which makes the `MkdirAll` a no-op.
- `internal/publicip/fs.go` chowns the written public-IP file to `PUID:PGID` (default `1000`),
  which needs `CAP_CHOWN`. Both `PUID` and `PGID` are set to `0` so the chown targets the file's
  existing owner, a permitted no-op. `PUID` otherwise only feeds the unused OpenVPN process user,
  since this tunnel is WireGuard.

## The blocklist and the memory limit

`BLOCK_MALICIOUS` defaults to `true` and is a live filter, not dormant config. It stays on for
qbittorrent, sabnzbd and prowlarr, whose resolvers see real tracker and indexer traffic. It is off
only here, because nginx's SNI map already drops every lookup that isn't `.imgur.com` before a
connection opens, so the blocklist download has nothing left to filter.

Turning it off was also the main response to an OOM crashloop: the sidecar OOMKilled 240+ times at
a 256Mi limit. cAdvisor never sampled this container above ~86Mi in 7 days (steady state ~26Mi),
because the spike lands about 2 seconds into startup and dies inside one scrape interval, invisible
to sampling. The 512Mi limit in the manifest is insurance rather than a proven fix: the unmeasured
7MB `servers.json` parse is a second candidate the blocklist change does not rule out. The other
three gluetun sidecars set no memory limit at all.

## The probe restart loop

`imgur-proxy` was, for a period, the only one of the four gluetun sidecars in `downloads` with
kubelet probes enabled on gluetun's health server. That probe restarts the _container_, but gluetun
keeps the pod's network namespace across container restarts and re-appends its firewall rules on
every start, so one transient tunnel failure became self-sustaining: probe failure, restart, more
accumulated netns state, a slower or failed start, probe failure again. This container sat in that
loop for over 4 days and 1352 restarts, ending in OOMKills, and only a pod recreation broke it.

Both gluetun probes are now disabled, matching the other three sidecars. `HEALTH_SERVER_DISABLE_LOOP`
is set to `off`, which means gluetun's own internal healthcheck loop stays enabled: it restarts the
_tunnel_ in-process on failure, recovering the same faults without touching the netns.
