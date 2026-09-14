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
    if out=$("$@" 2>/tmp/health-err.$$) && [ -n "$out" ] && jq -e . >/dev/null 2>&1 <<<"$out"; then
        parts+=("$(jq -n --arg k "$key" --argjson v "$out" '{($k): $v}')")
    else
        # Covers a non-zero exit, an empty body from a proxy, and non-JSON output:
        # all three are "did not run", never "nothing wrong".
        err=$(head -c 400 /tmp/health-err.$$ | tr -d '\000')
        [ -n "$err" ] || err="empty or non-JSON output"
        parts+=("$(jq -n --arg k "$key" --arg e "$err" '{($k): {blind: true, error: $e}}')")
    fi
    rm -f /tmp/health-err.$$
}

# Node names are internal identifiers (this repository is public), so the
# snapshot carries counts, never names. Look a cordoned node up by hand.
nodes() {
    kubectl get nodes -o json | jq '{
        ready: [.items[] | select(.status.conditions[] | select(.type=="Ready" and .status=="True"))] | length,
        total: (.items | length),
        cordoned: [.items[] | select(.spec.unschedulable == true)] | length,
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
        not_ready: [.items[] | select(.status.phase == "Running")
            | select(([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length) == 0)
            | {ns: .metadata.namespace, name: .metadata.name, since: ([.status.conditions[]? | select(.type == "Ready")][0].lastTransitionTime // null)}],
        restarts: ([.items[] | {key: (.metadata.namespace + "/" + .metadata.name),
            value: ((.status.containerStatuses // []) | map(.restartCount) | add // 0)}
            | select(.value > 0)] | from_entries)
    }'
}

# Presence guards: this cluster always has ExternalSecrets, ReplicationSources
# and Gatus series. Zero of any of them means the read path is broken, not that
# everything is healthy, so those cases return non-zero and record as blind.
eso() {
    local store all es
    store=$(kubectl get clustersecretstore onepassword-connect -o json | jq '[.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length > 0') || return 1
    all=$(kubectl get externalsecrets -A -o json) || return 1
    [ "$(jq '.items | length' <<<"$all")" -gt 0 ] || {
        echo "no ExternalSecrets returned" >&2
        return 1
    }
    es=$(jq '[.items[] | select(([.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length) == 0) | .metadata.namespace + "/" + .metadata.name]' <<<"$all")
    jq -n --argjson s "$store" --argjson e "$es" --argjson n "$(jq '.items | length' <<<"$all")" '{store_ready: $s, total: $n, not_synced: $e}'
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
    local all
    all=$(kubectl get replicationsource -A -o json) || return 1
    [ "$(jq '.items | length' <<<"$all")" -gt 0 ] || {
        echo "no ReplicationSources returned" >&2
        return 1
    }
    # stale: a source whose last successful sync is older than about twice its
    # schedule (NFS/kopia every 4 h -> 9 h; R2/restic nightly -> 30 h). A backup
    # path that quietly stops leaves every other field looking healthy.
    jq 'now as $n | {
        total: (.items | length),
        stale: [.items[] | (if .spec.kopia then 9 elif .spec.restic then 30 else 30 end) as $h
            | select((.status.lastSyncTime == null) or (($n - (.status.lastSyncTime | fromdateiso8601)) > ($h * 3600)))
            | .metadata.namespace + "/" + .metadata.name],
        synchronizing: [.items[] | select(.status.conditions[]? | select(.type=="Synchronizing" and .status=="True")) | .metadata.namespace + "/" + .metadata.name],
        last_failed: [.items[] | select(.status.latestMoverStatus.result? == "Failed") | .metadata.namespace + "/" + .metadata.name]
    }' <<<"$all"
}

gatus() {
    local base total failing
    base="/api/v1/namespaces/observability/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query"
    total=$(kubectl get --raw "${base}?query=count(gatus_results_endpoint_success)" | jq -r '.data.result[0].value[1] // "0"') || return 1
    [ "$total" -gt 0 ] 2>/dev/null || {
        echo "no gatus_results_endpoint_success series in Prometheus" >&2
        return 1
    }
    failing=$(kubectl get --raw "${base}?query=gatus_results_endpoint_success%7Bgroup!%3D%22connectivity%22%7D%20%3D%3D%200" |
        jq '[.data.result[] | (.metric.group // "-") + "/" + .metric.name] | sort') || return 1
    jq -n --argjson t "$total" --argjson f "$failing" '{total: $t, failing: $f}'
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
