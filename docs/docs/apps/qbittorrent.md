# qBittorrent

## Purpose

The home BitTorrent client in the `downloads` namespace. It runs behind the VPN sidecar, with a
`metrics` sidecar exporting qBittorrent state to Prometheus.

## Memory limit

The app container requests 512Mi and is limited to 16Gi. The disk cache is pinned to 256 MiB by
`apply-settings.sh`, but resident memory still grows with the number of torrents and files loaded,
and a large batch can push it far past the steady state.

| Date              | Limit | What happened                                                                        |
| ----------------- | ----- | ------------------------------------------------------------------------------------ |
| 2026-10-24        | 2Gi   | Six whole-platform ROM torrents (about 18,500 files) reached 1.2Gi; OOM-killed twice |
| 2026-10-28 07:46Z | 8Gi   | RSS reached the limit; OOM-killed                                                    |
| 2026-11-02 02:10Z | 8Gi   | RSS reached the limit; OOM-killed                                                    |

Over the 7 days to 2026-11-02, the Prometheus maximum of `container_memory_rss` was 8.34e9 bytes,
against a steady state of about 0.6–0.8 GiB, while the six ROM torrents were loaded. Nodes sat at
16–26% memory used, so a 16Gi limit leaves ample headroom. Requests stay at 512Mi so scheduling is
unchanged.

If the container is OOM-killed again, check `container_memory_rss` for the `qbittorrent` container
and the torrent count before raising the limit further.
