#!/bin/sh
# Backrest indexes snapshots only after its own backups or a click on "Index Snapshots", so repos
# seeded from config show an empty tree, and VolSync adds a snapshot to each one every night.
set -u

api=http://127.0.0.1:9898/v1.Backrest
until curl -sf -o /dev/null http://127.0.0.1:9898/; do sleep 5; done

while true; do
    failed=0
    sed -n 's/^ *"id": *"\([^"]*\)".*/\1/p' /config/config.json >/tmp/repos
    while read -r repo; do
        curl -sf -o /dev/null -X POST -H 'Content-Type: application/json' "$api/DoRepoTask" \
            -d "{\"repoId\": \"$repo\", \"task\": \"TASK_INDEX_SNAPSHOTS\"}" </dev/null || failed=$((failed + 1))
    done </tmp/repos
    echo "queued snapshot indexing; $failed request(s) failed"
    sleep 21600
done
