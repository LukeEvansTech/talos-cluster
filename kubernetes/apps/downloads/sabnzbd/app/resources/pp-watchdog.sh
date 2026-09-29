#!/bin/sh
# Kills a par2/unrar/7z helper that stalls, so it can't hang the whole post-processing queue
# behind it (docs/docs/troubleshooting/kb/059-sabnzbd-post-processing-helper-hang.md).
# A pipe write-blocked unrar is a hang (sabnzbd/sabnzbd#3638); pipe read-blocked is Direct
# Unpack waiting on the next volume, and is left alone.

STALL_SECONDS="${PP_WATCHDOG_STALL_SECONDS:-1800}"
# Progress below this within STALL_SECONDS counts as stalled: a hung par2 can still trickle
# ~0.5 MB every few minutes, which a "no I/O at all" rule never catches.
MIN_PROGRESS_BYTES="${PP_WATCHDOG_MIN_PROGRESS_BYTES:-16777216}"
INTERVAL_SECONDS="${PP_WATCHDOG_INTERVAL_SECONDS:-60}"
STATE_DIR="${PP_WATCHDOG_STATE_DIR:-/tmp/pp-watchdog}"
# Kills are also appended here, on the config volume: the container log rotates within about an
# hour under a busy queue, so a kill is gone from `kubectl logs` long before anyone looks.
KILL_LOG="${PP_WATCHDOG_KILL_LOG:-/config/pp-watchdog-kills.log}"
KILL_LOG_MAX_LINES=1000

log() {
    printf '%s pp-watchdog: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

log_kill() {
    log "$@"
    { log "$@" >>"$KILL_LOG"; } 2>/dev/null || return 0
    if [ "$(wc -l <"$KILL_LOG")" -gt "$KILL_LOG_MAX_LINES" ]; then
        tail -n $((KILL_LOG_MAX_LINES / 2)) "$KILL_LOG" >"$KILL_LOG.tmp" && mv "$KILL_LOG.tmp" "$KILL_LOG"
    fi
}

mkdir -p "$STATE_DIR" || exit 1
rm -f "$STATE_DIR"/*
log "started: stall=${STALL_SECONDS}s min_progress=${MIN_PROGRESS_BYTES}B interval=${INTERVAL_SECONDS}s kill_log=${KILL_LOG}"

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

        base_io=""
        since=$now
        if [ -f "$state" ]; then
            read -r base_io since <"$state"
        fi
        case "$wchan" in
        anon_pipe_read | pipe_read) base_io="" ;;
        esac
        if [ -z "$base_io" ] || [ $((io - base_io)) -ge "$MIN_PROGRESS_BYTES" ]; then
            echo "$io $now" >"$state"
            continue
        fi

        stalled=$((now - since))
        if [ "$stalled" -ge $((STALL_SECONDS + 5 * INTERVAL_SECONDS)) ]; then
            log_kill "SIGKILL $comm pid $pid: survived SIGTERM, <${MIN_PROGRESS_BYTES}B I/O in ${stalled}s"
            kill -KILL "$pid" 2>/dev/null
        elif [ "$stalled" -ge "$STALL_SECONDS" ]; then
            log_kill "SIGTERM $comm pid $pid: $((io - base_io))B I/O in ${stalled}s (wchan ${wchan:-?})"
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
