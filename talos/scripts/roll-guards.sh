#!/usr/bin/env bash
# Guards around a hand-driven rolling reboot: Ceph noout plus Alertmanager silences copied from the
# tuppr TalosUpgrade CR, so a manual roll silences exactly what a tuppr roll would.
# Usage: roll-guards.sh up|down   (state: $ROLL_STATE_DIR, default ~/.cache/talos-roll)
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
state=${ROLL_STATE_DIR:-$HOME/.cache/talos-roll}
mkdir -p "$state"
ids="$state/silence-ids"
owned="$state/noout-owned"
upgrade_cr="$repo/kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml"
tools() { kubectl -n rook-ceph exec deploy/rook-ceph-tools -- "$@"; }

# A kernel-chosen local port read back from kubectl, so the guards can only ever talk to the
# port-forward they started, never to another process already holding a fixed port.
pf_start() {
    pf_log=$(mktemp)
    kubectl -n observability port-forward svc/kube-prometheus-stack-alertmanager :9093 >"$pf_log" 2>&1 &
    pf=$!
    trap 'kill "$pf" 2>/dev/null; rm -f "$pf_log"' EXIT
    for _ in $(seq 1 20); do
        port=$(sed -nE 's/^Forwarding from 127\.0\.0\.1:([0-9]+) .*/\1/p' "$pf_log" | head -1)
        if [ -n "$port" ] && kill -0 "$pf" 2>/dev/null && curl -fs "http://127.0.0.1:$port/-/ready" >/dev/null; then
            am="http://127.0.0.1:$port"
            return 0
        fi
        sleep 1
    done
    echo "roll-guards: Alertmanager port-forward never became ready" >&2
    exit 1
}

case "${1:-}" in
up)
    # A noout someone else set stays theirs: down only unsets a flag these guards introduced. The
    # ownership mark is written before the flag, so a retried up after a later failure keeps it.
    if [ -f "$owned" ]; then
        tools ceph osd set noout
    elif tools ceph osd dump -f json | jq -e '.flags_set | index("noout")' >/dev/null; then
        echo "roll-guards: noout was already set by someone else; down will leave it set"
    else
        touch "$owned"
        tools ceph osd set noout
    fi
    pf_start
    start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # Portable: BSD date has no -d.
    end=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
    touch "$ids" # appended, never truncated: a retried up must not orphan earlier silences
    # One silence per CR entry; matchType =~ becomes isRegex, and the 3h window covers a full roll.
    yq -o=json '.spec.silences' "$upgrade_cr" | jq -c '.[]' | while read -r s; do
        body=$(jq -n --argjson s "$s" --arg st "$start" --arg en "$end" '{
            matchers: [$s.matchers[] | {name, value,
                isRegex: (.matchType == "=~" or .matchType == "!~"),
                isEqual: (.matchType == "=" or .matchType == "=~")}],
            startsAt: $st, endsAt: $en, createdBy: "roll-guards", comment: "hand-driven rolling reboot"}')
        curl -fsS -X POST -H 'Content-Type: application/json' -d "$body" \
            "$am/api/v2/silences" | jq -r .silenceID >>"$ids"
    done
    echo "roll-guards: noout in place, $(wc -l <"$ids" | tr -d ' ') silence(s) recorded, newest until $end"
    ;;
down)
    # Each guard is restored independently, PDB first, so one failure never skips another.
    rc=0
    # The CNPG operator recreates the PDB asynchronously after Flux restores enablePDB, so poll.
    flux reconcile kustomization cloudnative-pg-cluster -n database >/dev/null 2>&1 || true
    pdb() { kubectl -n database get pdb postgres18-primary -o jsonpath='{.status.disruptionsAllowed}' 2>/dev/null || true; }
    for _ in $(seq 1 24); do
        [ "$(pdb)" = 0 ] && break
        sleep 5
    done
    if [ "$(pdb)" = 0 ]; then
        echo "roll-guards: postgres18-primary PDB back at 0 allowed disruptions"
    else
        echo "roll-guards: postgres18-primary PDB is NOT back at 0 allowed disruptions after 2 min" >&2
        rc=1
    fi
    if [ -f "$owned" ]; then
        if tools ceph osd unset noout; then rm -f "$owned"; else rc=1; fi
    else
        echo "roll-guards: noout was not set by these guards; leaving it as it is"
    fi
    if [ -s "$ids" ]; then
        pf_start
        left=()
        while read -r id; do
            [ -n "$id" ] || continue
            if curl -fsS -X DELETE "$am/api/v2/silence/$id" >/dev/null; then
                echo "roll-guards: expired $id"
            else
                left+=("$id")
            fi
        done <"$ids"
        # Only the silences that failed to expire stay recorded, so a re-run retries them.
        printf '%s\n' "${left[@]}" | sed '/^$/d' >"$ids"
        if [ ${#left[@]} -gt 0 ]; then
            echo "roll-guards: ${#left[@]} silence(s) did not expire; run down again" >&2
            rc=1
        fi
    fi
    exit "$rc"
    ;;
*)
    echo "usage: roll-guards.sh up|down" >&2
    exit 2
    ;;
esac
