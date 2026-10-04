#!/bin/sh
# Pull finished Sonarr/Radarr seedbox grabs home (seedbox-apps apps/seedbox-pull).
#
# On the seedbox, qBittorrent hardlinks each finished sonarr-home / radarr-home
# torrent into an outbox served over SFTP. For every outbox entry this moves it
# into <stage>/.incoming/<category>/, then renames it to <stage>/<category>/ once
# the whole entry is home, so Sonarr/Radarr (remote path mapping
# /downloads/complete/<category>/ -> /media/downloads/seedbox/<category>/) never
# import a half-copied season pack. Moving deletes only the hardlinks; the torrent
# keeps seeding on the seedbox. A failed run leaves its partial copy in .incoming
# and the rest in the outbox, and the next run finishes it.
#
# Entries in <stage>/<category>/ are removed 14 days after they land. The arrs
# import by hardlink on the same NFS dataset, so this frees nothing the library
# still uses.
set -u
STAGE="${STAGE:-/downloads/seedbox}"
KEEP_DAYS="${KEEP_DAYS:-14}"
failed=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

for cat in sonarr-home radarr-home; do
    mkdir -p "$STAGE/$cat" "$STAGE/.incoming/$cat"
    # Top-level entries only; dot entries are the seedbox hook's staging dirs.
    if ! rclone lsf --max-depth 1 --exclude '.*' --exclude '.*/**' "seedbox:$cat" >/tmp/entries; then
        log "FAIL $cat: cannot list outbox"
        failed=1
        continue
    fi
    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        name="${entry%/}"
        if [ -e "$STAGE/$cat/$name" ]; then
            log "SKIP $cat/$name: already home (outbox entry left for inspection)"
            continue
        fi
        start=$(date +%s)
        if [ "$entry" != "$name" ]; then
            rclone move "seedbox:$cat/$name" "$STAGE/.incoming/$cat/$name" \
                --delete-empty-src-dirs --transfers 4 --multi-thread-streams 8 --stats 0 &&
                rclone rmdir "seedbox:$cat/$name"
        else
            rclone moveto "seedbox:$cat/$name" "$STAGE/.incoming/$cat/$name" \
                --multi-thread-streams 8 --stats 0
        fi
        rc=$?
        if [ "$rc" -eq 0 ] && mv "$STAGE/.incoming/$cat/$name" "$STAGE/$cat/$name"; then
            # Stamp arrival time for the prune below (rclone keeps the seedbox mtime).
            touch "$STAGE/$cat/$name"
            size=$(du -sm "$STAGE/$cat/$name" | cut -f1)
            secs=$(($(date +%s) - start))
            log "OK $cat/$name ${size}MiB in ${secs}s"
        else
            log "FAIL $cat/$name: rclone exit $rc (partial copy kept in .incoming)"
            failed=1
        fi
    done </tmp/entries
done

for cat in sonarr-home radarr-home; do
    find "$STAGE/$cat" -mindepth 1 -maxdepth 1 -mtime +"$KEEP_DAYS" | while IFS= read -r old; do
        rm -rf "$old" && log "PRUNED $old"
    done
done

exit "$failed"
