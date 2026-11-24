#!/bin/sh
# Writes backrest's config.json with one repository per app prefix under the VolSync R2 base, so
# the UI lists every remote backup, including those of retired apps, after each restart.
#
# Each guid is read with `restic cat config` rather than left to autoInitialize: backrest's
# auto-init runs `restic init` when the existence check fails, so one transient R2 error would
# create a repository in that prefix, and any init error leaves every repository unconfigured.
#
# Only automatic maintenance is off: the credentials can still write, so a backup or Run Command
# from the UI would. Every command gets --no-lock because a lock left by a restarted backrest pod
# would fail VolSync's next prune.
#
# Restic exit 10 means the prefix holds no repository and is skipped; any other failure is retried,
# then fails the init container so the kubelet retries instead of starting with repos missing.
set -eu

out=${1:-/config/config.json}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

base=${RESTIC_REPOSITORY_BASE#s3:}
endpoint=${base%/*}
bucket=${base##*/}

rclone lsf --dirs-only --s3-provider Cloudflare --s3-env-auth --s3-endpoint "$endpoint" \
    ":s3:$bucket" >"$work/dirs"
sed 's|/$||' "$work/dirs" >"$work/apps"

# shellcheck disable=SC2016 # expanded by the inner shell, once per app
xargs -P 8 -I{} sh -c '
    app=$1
    for try in 1 2 3; do
        rc=0
        cfg=$(restic -r "$RESTIC_REPOSITORY_BASE/$app" cat config --no-lock --no-cache 2>/dev/null) || rc=$?
        if [ "$rc" -eq 10 ]; then
            echo "skipped $app: no restic repository" >&2
            exit 0
        fi
        id=$(printf "%s" "$cfg" | sed -n "s/.*\"id\": *\"\([0-9a-f]\{64\}\)\".*/\1/p")
        if [ "$rc" -eq 0 ] && [ -n "$id" ]; then
            echo "$id" >"$2/$app.id"
            exit 0
        fi
        sleep $((try * 5))
    done
    echo "failed $app: restic exit $rc after 3 tries" >&2
    exit 1
' _ {} "$work" <"$work/apps"

sep=""
{
    printf '{"version": 6, "instance": "talos", "auth": {"disabled": true}, "repos": ['
    while read -r app; do
        case $app in "" | *[!a-zA-Z0-9_.-]*) continue ;; esac
        [ -f "$work/$app.id" ] || continue
        # shellcheck disable=SC2016 # backrest expands the uri itself, keeping the R2 endpoint out of the file
        printf '%s\n  {"id": "%s", "uri": "${RESTIC_REPOSITORY_BASE}/%s", "guid": "%s", "flags": ["--no-lock"], ' \
            "$sep" "$app" "$app" "$(cat "$work/$app.id")"
        printf '"prunePolicy": {"schedule": {"disabled": true}}, "checkPolicy": {"schedule": {"disabled": true}}}'
        sep=","
    done <"$work/apps"
    printf '\n]}\n'
} >"$out"

echo "wrote $(grep -c '"guid"' "$out") of $(wc -l <"$work/apps") prefixes to $out"
