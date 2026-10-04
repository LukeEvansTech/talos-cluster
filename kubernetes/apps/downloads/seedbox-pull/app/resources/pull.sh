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

# concurrencyPolicy only serialises scheduled runs; a manual
# `create job --from=cronjob` would race them over the same files. A lock
# older than the 6h activeDeadlineSeconds belongs to a killed run.
LOCK="$STAGE/.lock"
mkdir -p "$STAGE"
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin -370)" ]; then
        log "SKIP: another run holds $LOCK"
        exit 0
    fi
    log "taking over stale $LOCK"
    if ! { rm -rf "$LOCK" && mkdir "$LOCK"; }; then
        exit 1
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

for cat in sonarr-home radarr-home; do
    mkdir -p "$STAGE/$cat" "$STAGE/.incoming/$cat"
    # Top-level entries only; dot entries are the seedbox hook's staging dirs.
    if ! rclone lsf --max-depth 1 --exclude '.*' --exclude '.*/**' "seedbox:$cat" >/tmp/entries; then
        log "FAIL $cat: cannot list outbox"
        failed=1
        continue
    fi
    # An entry already gone from the outbox but still in .incoming was fully
    # moved by a run that died before publishing it.
    for inc in "$STAGE/.incoming/$cat"/*; do
        [ -e "$inc" ] || continue
        name="${inc##*/}"
        grep -qxF -e "$name" -e "$name/" /tmp/entries && continue
        if [ ! -e "$STAGE/$cat/$name" ] && mv "$inc" "$STAGE/$cat/$name"; then
            touch "$STAGE/$cat/$name"
            log "RECOVERED $cat/$name"
        else
            log "FAIL $cat/$name: stranded in .incoming"
            failed=1
        fi
    done
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
    if ! find "$STAGE/$cat" -mindepth 1 -maxdepth 1 -mtime +"$KEEP_DAYS" >/tmp/expired; then
        log "FAIL $cat: cannot scan for expired entries"
        failed=1
        continue
    fi
    while IFS= read -r old; do
        if rm -rf "$old"; then
            log "PRUNED $old"
        else
            log "FAIL prune $old"
            failed=1
        fi
    done </tmp/expired
done

exit "$failed"
