#!/bin/sh
# Kill SABnzbd post-processing helpers (par2, unrar, 7z) that have stopped
# making progress.
#
# SABnzbd post-processes one job at a time and has no timeout on its helpers,
# so a single hung helper stalls every job queued behind it. In September 2026
# a par2 repair spun on one core for ~30 h without reading a byte while 3,800
# downloaded jobs waited. SABnzbd's own mode=cancel_pp API cannot be relied on
# to recover: PostProcessor.cancel_pp returns after checking only the first job
# in its list, so it misses the active job whenever another job is ahead of it.
#
# Progress is rchar + wchar from /proc/<pid>/io. A helper whose counters have
# not moved for STALL_SECONDS is sent SIGTERM (then SIGKILL if it survives);
# SABnzbd marks that one job failed, Sonarr/Radarr blocklist the release and
# search again, and post-processing moves on to the next job.
#
# A helper blocked reading a pipe is left alone: that is Direct Unpack's unrar
# waiting for SABnzbd to hand it the next volume, not a hang. unrar blocked
# *writing* a pipe is a hang (sabnzbd/sabnzbd#3638) and is killed.
#
# Runs in the background inside the SABnzbd container (see helmrelease.yaml),
# so it sees the helpers without sharing the pod's process namespace.

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
