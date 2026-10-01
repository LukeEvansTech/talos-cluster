#!/usr/bin/env bash
# Guards around a hand-driven rolling reboot: Ceph noout plus Alertmanager silences copied from the
# tuppr TalosUpgrade CR, so a manual roll silences exactly what a tuppr roll would.
# Usage: roll-guards.sh up|down   (state: $ROLL_STATE_DIR, default ~/.cache/talos-roll)
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
state=${ROLL_STATE_DIR:-$HOME/.cache/talos-roll}
mkdir -p "$state"
ids="$state/silence-ids"
upgrade_cr="$repo/kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml"
tools() { kubectl -n rook-ceph exec deploy/rook-ceph-tools -- "$@"; }

pf_start() {
    kubectl -n observability port-forward svc/kube-prometheus-stack-alertmanager 19093:9093 >/dev/null 2>&1 &
    pf=$!
    trap 'kill "$pf" 2>/dev/null' EXIT
    for _ in $(seq 1 20); do
        curl -fs http://127.0.0.1:19093/-/ready >/dev/null && return 0
        sleep 1
    done
    echo "roll-guards: Alertmanager port-forward never became ready" >&2
    exit 1
}

case "${1:-}" in
up)
    tools ceph osd set noout
    pf_start
    start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    end=$(date -u -d '+3 hours' +%Y-%m-%dT%H:%M:%SZ)
    : >"$ids"
    # One silence per CR entry; matchType =~ becomes isRegex, and the 3h window covers a full roll.
    yq -o=json '.spec.silences' "$upgrade_cr" | jq -c '.[]' | while read -r s; do
        body=$(jq -n --argjson s "$s" --arg st "$start" --arg en "$end" '{
            matchers: [$s.matchers[] | {name, value,
                isRegex: (.matchType == "=~" or .matchType == "!~"),
                isEqual: (.matchType == "=" or .matchType == "=~")}],
            startsAt: $st, endsAt: $en, createdBy: "roll-guards", comment: "hand-driven rolling reboot"}')
        curl -fsS -X POST -H 'Content-Type: application/json' -d "$body" \
            http://127.0.0.1:19093/api/v2/silences | jq -r .silenceID >>"$ids"
    done
    echo "roll-guards: noout set, $(wc -l <"$ids" | tr -d ' ') silences until $end"
    ;;
down)
    tools ceph osd unset noout
    pf_start
    if [ -f "$ids" ]; then
        while read -r id; do
            [ -n "$id" ] && curl -fsS -X DELETE "http://127.0.0.1:19093/api/v2/silence/$id" && echo "roll-guards: expired $id"
        done <"$ids"
        rm -f "$ids"
    fi
    # Restore the CNPG primary PDB that roll-node.sh relaxes before each drain.
    flux reconcile kustomization cloudnative-pg-cluster -n database >/dev/null 2>&1
    echo "roll-guards: noout unset; postgres18 PDBs: $(kubectl -n database get pdb -o jsonpath='{range .items[*]}{.metadata.name}={.status.disruptionsAllowed} {end}')"
    ;;
*)
    echo "usage: roll-guards.sh up|down" >&2
    exit 2
    ;;
esac
