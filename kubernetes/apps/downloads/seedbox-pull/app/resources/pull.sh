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
# `create job --from=cronjob` would race them over the same files.
LOCK="$STAGE/.lock"
TOKEN="$(hostname)-$$"
mkdir -p "$STAGE"
# Age is read from the owner file, written once per holder; a dir's mtime
# changes whenever an entry is added inside it.
fresh() { [ -n "$(find "$1" -maxdepth 0 -mmin -370 2>/dev/null)" ]; }
if ! mkdir "$LOCK" 2>/dev/null; then
    ref="$LOCK/owner"
    [ -e "$ref" ] || ref="$LOCK"
    if fresh "$ref"; then
        log "SKIP: another run holds $LOCK"
        exit 0
    fi
    # Older than the job's 6h activeDeadlineSeconds, so its holder is dead: a
    # SIGKILL (OOM) skips the EXIT trap. Claim it in place: only one run can
    # mkdir the takeover dir, and it stays until the lock is released, so the
    # path is never reopened for a second taker.
    if ! mkdir "$LOCK/takeover" 2>/dev/null; then
        log "SKIP: another run is taking over $LOCK"
        exit 0
    fi
    # The lock may have been released and re-created by a live run between
    # the age check and the claim: back off unless its owner is still stale.
    if [ ! -e "$LOCK/owner" ] || fresh "$LOCK/owner"; then
        rmdir "$LOCK/takeover" 2>/dev/null
        log "SKIP: $LOCK changed hands during takeover"
        exit 0
    fi
    log "TOOK OVER: $LOCK was older than the 6h deadline"
fi
echo "$TOKEN" >"$LOCK/owner"
# shellcheck disable=SC2329 # called by the EXIT trap
release() {
    if [ "$(cat "$LOCK/owner" 2>/dev/null)" = "$TOKEN" ]; then
        rm -rf "$LOCK"
    fi
}
trap release EXIT
trap 'exit 143' TERM INT
# sh defers traps until a foreground child exits; backgrounding rclone and
# waiting lets SIGTERM interrupt the wait and release the lock in time.
run() {
    "$@" &
    wait $!
}

for cat in sonarr-home radarr-home; do
    mkdir -p "$STAGE/$cat" "$STAGE/.incoming/$cat"
    # Top-level entries only; dot entries are the seedbox hook's staging dirs.
    if ! run rclone lsf --max-depth 1 --exclude '.*' --exclude '.*/**' "seedbox:$cat" >/tmp/entries; then
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
            run rclone move "seedbox:$cat/$name" "$STAGE/.incoming/$cat/$name" \
                --delete-empty-src-dirs --transfers 4 --multi-thread-streams 4 --stats 0 &&
                run rclone rmdir "seedbox:$cat/$name"
        else
            run rclone moveto "seedbox:$cat/$name" "$STAGE/.incoming/$cat/$name" \
                --multi-thread-streams 4 --stats 0
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
