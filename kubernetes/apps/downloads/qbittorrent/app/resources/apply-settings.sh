#!/bin/sh
# Writes the settings below into qBittorrent.conf before qBittorrent starts, so they
# are declared in Git and survive a PVC recreate. qBittorrent only maps QBT_* env vars
# onto its command-line options (--webui-port, --torrenting-port, ...), so
# QBT_<Section>__<Key> variables never reached the config file.
# qBittorrent rewrites the file on exit, so a change made in the WebUI lasts only until
# the next pod start. Change it here instead.

set -eu

conf=/config/qBittorrent/qBittorrent.conf
mkdir -p /config/qBittorrent
[ -f "$conf" ] || cp /defaults/qBittorrent.conf "$conf"

# <section>|<key>|<value>, one per line.
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

# Replace the key if present, else add it at the end of its section (creating the
# section if missing). Values reach awk through ENVIRON, not -v, because -v would
# strip the backslash from keys like Session\DiskCacheSize.
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
