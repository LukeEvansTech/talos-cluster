#!/bin/sh
# Kills a par2/unrar/7z helper that stalls, so it can't hang the whole post-processing queue
# behind it (docs/docs/troubleshooting/kb/059-sabnzbd-post-processing-helper-hang.md).
# A pipe write-blocked unrar is a hang (sabnzbd/sabnzbd#3638); pipe read-blocked is Direct
# Unpack waiting on the next volume, and is left alone.

STALL_SECONDS="${PP_WATCHDOG_STALL_SECONDS:-1800}"
INTERVAL_SECONDS="${PP_WATCHDOG_INTERVAL_SECONDS:-60}"
STATE_DIR="${PP_WATCHDOG_STATE_DIR:-/tmp/pp-watchdog}"

log() {
    printf '%s pp-watchdog: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

mkdir -p "$STATE_DIR" || exit 1
rm -f "$STATE_DIR"/*
log "started: stall=${STALL_SECONDS}s interval=${INTERVAL_SECONDS}s"

while :; do
    now=$(date +%s)
    seen=" "
    for dir in /proc/[0-9]*; do
        comm=$(cat "$dir/comm" 2>/dev/null) || continue
        case "$comm" in
        par2* | unrar* | 7z*) ;;
        *) continue ;;
        esac
        # Zombies and processes owned by another user have no readable io.
        io=$(awk '/^(rchar|wchar):/ { n += $2 } END { print n }' "$dir/io" 2>/dev/null) || continue
        [ -n "$io" ] || continue
        wchan=$(cat "$dir/wchan" 2>/dev/null)
        pid=${dir#/proc/}
        # pid plus start time, so a recycled pid never inherits old state.
        key="$pid-$(awk '{ print $22 }' "$dir/stat" 2>/dev/null)"
        seen="$seen$key "
        state="$STATE_DIR/$key"

        last_io=""
        since=$now
        if [ -f "$state" ]; then
            read -r last_io since <"$state"
        fi
        case "$wchan" in
        anon_pipe_read | pipe_read) last_io="" ;;
        esac
        if [ "$io" != "$last_io" ]; then
            echo "$io $now" >"$state"
            continue
        fi

        stalled=$((now - since))
        if [ "$stalled" -ge $((STALL_SECONDS + 5 * INTERVAL_SECONDS)) ]; then
            log "SIGKILL $comm pid $pid: survived SIGTERM, no I/O for ${stalled}s"
            kill -KILL "$pid" 2>/dev/null
        elif [ "$stalled" -ge "$STALL_SECONDS" ]; then
            log "SIGTERM $comm pid $pid: no I/O for ${stalled}s (wchan ${wchan:-?})"
            kill -TERM "$pid" 2>/dev/null
        fi
    done

    for state in "$STATE_DIR"/*; do
        [ -e "$state" ] || continue
        case "$seen" in
        *" ${state##*/} "*) ;;
        *) rm -f "$state" ;;
        esac
    done

    sleep "$INTERVAL_SECONDS"
done
