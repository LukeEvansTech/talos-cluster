#!/usr/bin/env bash
set -uo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the repo kubeconfig}"
export KUBECONFIG

parts=()
run() {
    local key=$1
    shift
    local out err
    if out=$("$@" 2>/tmp/health-err.$$) && [ -n "$out" ] && jq -e . >/dev/null 2>&1 <<<"$out"; then
        parts+=("$(jq -n --arg k "$key" --argjson v "$out" '{($k): $v}')")
    else
        # A non-zero exit, an empty proxy body, and non-JSON output are all "did not run",
        # never "nothing wrong".
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

# stderr is left alone so a flux failure surfaces as blind, never as a bogus "not ready" entry.
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
            value: (((.status.containerStatuses // []) + (.status.initContainerStatuses // [])) | map(.restartCount) | add // 0)}
            | select(.value > 0)] | from_entries)
    }'
}

# Presence guards: this cluster always has ExternalSecrets, ReplicationSources and Gatus series.
# Zero of any means the read path is broken, not a healthy cluster, so it returns blind.
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

# The always-firing Watchdog proves Prometheus still evaluates rules and delivers to Alertmanager.
# Without it, an empty critical list means the pipeline is dead, not that the cluster is quiet.
alerts() {
    local base wd crit
    base="/api/v1/namespaces/observability/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts"
    wd=$(kubectl get --raw "${base}?active=true&filter=alertname%3DWatchdog" | jq 'length') || return 1
    [ "$wd" -gt 0 ] 2>/dev/null || {
        echo "Watchdog alert not active in Alertmanager: the alerting pipeline is down" >&2
        return 1
    }
    crit=$(kubectl get --raw "${base}?active=true&silenced=false&filter=severity%3Dcritical" |
        jq '[.[] | {alertname: .labels.alertname, namespace: (.labels.namespace // null), startsAt: .startsAt}] | sort_by(.alertname)') || return 1
    jq -n --argjson w true --argjson c "$crit" '{watchdog: $w, critical: $c}'
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
    # stale: no sync in over 9h for kopia (~2x its 4h schedule) or 30h for restic (nightly).
    # A backup that quietly stops otherwise looks healthy on every other field.
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
    # Guard on sidecar-discovered endpoints (group internal|external, from the Gateway annotation),
    # not the total: six static endpoints in config.yaml keep that non-zero even if discovery dies.
    total=$(kubectl get --raw "${base}?query=count(gatus_results_endpoint_success%7Bgroup%3D~%22internal%7Cexternal%22%7D)" | jq -r '.data.result[0].value[1] // "0"') || return 1
    [ "$total" -gt 0 ] 2>/dev/null || {
        echo "no sidecar-discovered Gatus endpoints (group internal|external) in Prometheus: discovery is down" >&2
        return 1
    }
    failing=$(kubectl get --raw "${base}?query=gatus_results_endpoint_success%7Bgroup!%3D%22connectivity%22%7D%20%3D%3D%200" |
        jq '[.data.result[] | (.metric.group // "-") + "/" + .metric.name] | sort') || return 1
    jq -n --argjson t "$total" --argjson f "$failing" '{discovered: $t, failing: $f}'
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
