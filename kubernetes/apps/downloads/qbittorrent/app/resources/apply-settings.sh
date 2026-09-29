#!/bin/sh
# QBT_<Section>__<Key> env vars are never mapped; qBittorrent only reads QBT_* as CLI
# options. qBittorrent also rewrites this file on exit, so change settings here, not in
# the WebUI: a WebUI change lasts only until the next pod start.

set -eu

conf=/config/qBittorrent/qBittorrent.conf
mkdir -p /config/qBittorrent
[ -f "$conf" ] || cp /defaults/qBittorrent.conf "$conf"

# Every torrent here is on a public tracker and home upload is very limited, so ratio and
# seeding time aren't tracked: torrents stop on completion and upload stays capped low.
settings=$(
    cat <<'EOF'
BitTorrent|Session\DiskCacheSize|256
BitTorrent|Session\QueueingSystemEnabled|true
BitTorrent|Session\MaxActiveDownloads|10
BitTorrent|Session\MaxActiveTorrents|20
BitTorrent|Session\IgnoreSlowTorrentsForQueueing|true
BitTorrent|Session\GlobalUPSpeedLimit|10
BitTorrent|Session\IncludeOverheadInLimits|false
BitTorrent|Session\GlobalMaxRatio|0
BitTorrent|Session\GlobalMaxSeedingMinutes|0
BitTorrent|Session\ShareLimitAction|Stop
Preferences|WebUI\LocalHostAuth|true
Preferences|WebUI\AuthSubnetWhitelistEnabled|false
EOF
)

# ENVIRON, not -v: -v strips the backslash in keys like Session\DiskCacheSize.
printf '%s\n' "$settings" | while IFS='|' read -r section key value; do
    [ -n "$section" ] || continue
    SECTION="[$section]" KEY="$key=" VALUE="$value" awk '
        BEGIN { s = ENVIRON["SECTION"]; k = ENVIRON["KEY"]; v = ENVIRON["VALUE"] }
        $0 == s { in_s = 1; seen = 1; print; next }
        /^\[/ { if (in_s && !done) { print k v; done = 1 } in_s = 0 }
        in_s && index($0, k) == 1 { if (!done) print k v; done = 1; next }
        { print }
        END { if (!done) { if (!seen) print s; print k v } }
    ' "$conf" >"$conf.tmp"
    mv "$conf.tmp" "$conf"
    echo "qbittorrent.conf: [$section] $key=$value"
done
