#!/usr/bin/env bash
# Read-only cluster health snapshot. Prints one JSON document on stdout.
#
# Every check is isolated: a command that fails records {"blind": true, "error": "..."}
# for its key instead of aborting the run, because a check that did not run must
# never read as green. See docs/docs/operations/health-verdict.md for what each
# field means and how to classify it.
#
# Requires: kubectl, flux, jq, and KUBECONFIG pointing at the cluster.
set -uo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the repo kubeconfig}"
export KUBECONFIG

# run KEY CMD... : capture stdout as a JSON string under KEY, or a blind marker on failure.
# Output is accumulated into $parts (one JSON object fragment per check).
parts=()
run() {
    local key=$1
    shift
    local out err
    if out=$("$@" 2>/tmp/health-err.$$); then
        parts+=("$(jq -n --arg k "$key" --argjson v "$out" '{($k): $v}')")
    else
        err=$(head -c 400 /tmp/health-err.$$ | tr -d '\000')
        parts+=("$(jq -n --arg k "$key" --arg e "$err" '{($k): {blind: true, error: $e}}')")
    fi
    rm -f /tmp/health-err.$$
}

nodes() {
    kubectl get nodes -o json | jq '{
        ready: [.items[] | select(.status.conditions[] | select(.type=="Ready" and .status=="True"))] | length,
        total: (.items | length),
        cordoned: [.items[] | select(.spec.unschedulable == true) | .metadata.name],
        versions: [.items[].status.nodeInfo.kubeletVersion] | unique,
        os: [.items[].status.nodeInfo.osImage] | unique
    }'
}

# flux_list KIND : "ns/name" JSON array of not-ready objects of KIND, or non-zero if flux failed.
# stderr is left alone so a failure surfaces as blind, never as a bogus "not ready" entry.
flux_list() {
    local raw
    raw=$(flux get "$@" -A --status-selector ready=false --no-header) || return 1
    printf '%s\n' "$raw" | awk 'NF{print $1"/"$2}' | jq -R . | jq -s .
}

flux_state() {
    local ks hr src
    ks=$(flux_list kustomizations) || return 1
    hr=$(flux_list helmreleases) || return 1
    src=$(flux_list sources all) || return 1
    jq -n --argjson ks "$ks" --argjson hr "$hr" --argjson src "$src" \
        '{ks_not_ready: $ks, hr_not_ready: $hr, src_not_ready: $src}'
}

pods() {
    kubectl get pods -A -o json | jq '{
        not_running: [.items[] | select(.status.phase != "Running" and .status.phase != "Succeeded")
            | {ns: .metadata.namespace, name: .metadata.name, phase: .status.phase, created: .metadata.creationTimestamp}],
        waiting: [.items[] | select(.status.phase == "Running")
            | . as $p | (.status.containerStatuses // [])[] | select(.state.waiting != null)
            | {ns: $p.metadata.namespace, name: $p.metadata.name, reason: .state.waiting.reason}],
        restarts: ([.items[] | {key: (.metadata.namespace + "/" + .metadata.name),
            value: ((.status.containerStatuses // []) | map(.restartCount) | add // 0)}
            | select(.value > 0)] | from_entries)
    }'
}

eso() {
    local store es
    store=$(kubectl get clustersecretstore onepassword-connect -o json | jq '[.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length > 0')
    es=$(kubectl get externalsecrets -A -o json | jq '[.items[] | select(([.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length) == 0) | .metadata.namespace + "/" + .metadata.name]')
    jq -n --argjson s "$store" --argjson e "$es" '{store_ready: $s, not_synced: $e}'
}

alerts() {
    kubectl get --raw "/api/v1/namespaces/observability/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts?active=true&silenced=false&filter=severity%3Dcritical" |
        jq '{critical: [.[] | {alertname: .labels.alertname, namespace: (.labels.namespace // null), startsAt: .startsAt}] | sort_by(.alertname)}'
}

ceph() {
    local health osd
    health=$(kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph health) || return 1
    osd=$(kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd stat) || return 1
    jq -n --arg h "$health" --arg o "$osd" '{health: ($h | split(" ")[0]), detail: $h, osd: $o}'
}

volsync() {
    kubectl get replicationsource -A -o json | jq '{
        synchronizing: [.items[] | select(.status.conditions[]? | select(.type=="Synchronizing" and .status=="True")) | .metadata.namespace + "/" + .metadata.name],
        last_failed: [.items[] | select(.status.latestMoverStatus.result? == "Failed") | .metadata.namespace + "/" + .metadata.name]
    }'
}

gatus() {
    kubectl get --raw "/api/v1/namespaces/observability/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=gatus_results_endpoint_success%7Bgroup!%3D%22connectivity%22%7D%20%3D%3D%200" |
        jq '{failing: [.data.result[] | (.metric.group // "-") + "/" + .metric.name] | sort}'
}

run nodes nodes
run flux flux_state
run pods pods
run eso eso
run alerts alerts
run ceph ceph
run volsync volsync
run gatus gatus

taken=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "${parts[@]}" | jq -s --arg t "$taken" 'add + {taken: $t}'
