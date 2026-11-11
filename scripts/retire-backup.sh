#!/usr/bin/env bash
# Stop an app being retired and take a fresh VolSync sync of every destination, so the backup
# postdates the app's last write. Runbook: docs/docs/operations/retiring-an-app.md, step 1.
# Usage: retire-backup.sh <namespace> <kustomization>...  (every Kustomization in the app's ks.yaml)
# RETIRE_TIMEOUT (seconds, default 1800) bounds all the waits together.
set -euo pipefail

ns=${1:?namespace}
shift
kss=("$@")
[ "${#kss[@]}" -gt 0 ] || {
    echo "usage: $0 <namespace> <kustomization>..." >&2
    exit 2
}

# Every change made here, so cleanup can put it back: no HelmRelease here enables drift
# detection, so resuming one does not restore replicas or CronJobs.
scaled=()    # "kind/name replicas"
suspended=() # "cronjob/name"
paused=()    # "hr name" / "ks name", resumed HelmReleases first

# Restore what was stopped, then resume Flux: it skips the prune for a Kustomization or
# HelmRelease deleted while suspended. Returns nonzero if anything could not be put back.
cleanup() {
    local rc=0 entry k
    for entry in "${scaled[@]+"${scaled[@]}"}"; do
        kubectl -n "$ns" scale "${entry% *}" --replicas="${entry##* }" >/dev/null || {
            echo "RESTORE FAILED: scale ${entry% *} to ${entry##* }" >&2
            rc=1
        }
    done
    for entry in "${suspended[@]+"${suspended[@]}"}"; do
        kubectl -n "$ns" patch "$entry" -p '{"spec":{"suspend":false}}' >/dev/null || {
            echo "RESTORE FAILED: unsuspend $entry" >&2
            rc=1
        }
    done
    for entry in "${paused[@]+"${paused[@]}"}"; do
        flux resume "${entry% *}" "${entry##* }" -n "$ns" >/dev/null || {
            echo "RESUME FAILED: $entry" >&2
            rc=1
        }
    done
    return "$rc"
}
# shellcheck disable=SC2329 # invoked by the EXIT trap
on_exit() {
    cleanup || echo "CLEANUP INCOMPLETE: restore the app by hand before merging" >&2
}
# The HelmReleases each Kustomization applies; their names need not match (memini-models applies
# llama-embed and llama-rerank). Assigned on their own lines so a failed query stops the run.
hrs=()
for k in "${kss[@]}"; do
    inv=$(kubectl -n "$ns" get kustomization "$k" -o jsonpath='{range .status.inventory.entries[*]}{.id}{"\n"}{end}')
    while IFS=_ read -r hns hname _; do
        [ "$hns" = "$ns" ] || {
            echo "REFUSED: $k applies HelmRelease $hname in $hns; retire each namespace separately" >&2
            exit 1
        }
        hrs+=("$hname")
    done < <(grep '_helm.toolkit.fluxcd.io_HelmRelease$' <<<"$inv" || true)
done
hrs_json=$(printf '%s\n' "${hrs[@]+"${hrs[@]}"}" | jq -R 'select(. != "")' | jq -sc .)
kss_json=$(printf '%s\n' "${kss[@]}" | jq -R . | jq -sc .)
# A controller is the app's if one of its HelmReleases or, for plain manifests, one of its
# Kustomizations applied it.
# shellcheck disable=SC2016 # jq variables, not shell ones
mine_jq='def mine: ((.metadata.labels["helm.toolkit.fluxcd.io/name"] // "") | IN($h[]))
    or ((.metadata.labels["kustomize.toolkit.fluxcd.io/name"] // "") | IN($k[]));'

# Every claim the app's controllers mount, StatefulSet claims included: each needs a backup.
controllers=$(kubectl get deploy,statefulset,cronjob -n "$ns" -o json)
pvcs=$(kubectl get pvc -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
claims_txt=$(
    jq -r --argjson h "$hrs_json" --argjson k "$kss_json" "$mine_jq"'
        .items[] | select(mine)
        | (.spec.template.spec // .spec.jobTemplate.spec.template.spec).volumes[]?
        | .persistentVolumeClaim.claimName // empty' <<<"$controllers"
    jq -r --argjson h "$hrs_json" --argjson k "$kss_json" "$mine_jq"'
        .items[] | select(.kind == "StatefulSet")
        | select(mine)
        | .metadata.name as $s | .spec.volumeClaimTemplates[]?.metadata.name | "^\(.)-\($s)-[0-9]+$"' \
        <<<"$controllers" | while read -r re; do grep -E "$re" <<<"$pvcs" || true; done
)
claims_txt=$(sort -u <<<"$claims_txt" | sed '/^$/d')
if [ -z "$claims_txt" ]; then
    echo "REFUSED: found no claim mounted by the app's controllers; if it is stateless it needs no" >&2
    echo "backup, otherwise find its claims by hand (runbook step 1)." >&2
    exit 1
fi
claims=$(jq -R . <<<"$claims_txt" | jq -sc 'map(select(. != ""))')

# Refuse before changing anything if a running controller outside these HelmReleases mounts a
# claim: another app sharing it (curatorjudge writes to curator's) would keep writing.
foreign=$(jq -r --argjson h "$hrs_json" --argjson k "$kss_json" --argjson c "$claims" "$mine_jq"'
    .items[]
    | (.spec.template.spec // .spec.jobTemplate.spec.template.spec) as $pod
    | select(any($pod.volumes[]?; .persistentVolumeClaim.claimName | IN($c[])))
    | select(mine | not)
    | select(.spec.suspend != true and (.spec.replicas // 1) > 0)
    | "\(.kind)/\(.metadata.name)"' <<<"$controllers")
if [ -n "$foreign" ]; then
    echo "REFUSED: other running controllers mount the claim; suspend or scale them to 0 first:" >&2
    awk '{ print "    " $0 }' <<<"$foreign" >&2
    exit 1
fi

# Every wait shares one deadline, so a writer that can never finish (an ImageMaid Job that needs
# Plex running) fails the run, and the EXIT trap restores the app, instead of hanging.
deadline=$(($(date +%s) + ${RETIRE_TIMEOUT:-1800}))
tick() {
    [ "$(date +%s)" -lt "$deadline" ] || {
        echo "TIMED OUT waiting for: $1" >&2
        exit 1
    }
    sleep 10
}

trap on_exit EXIT

for k in "${kss[@]}"; do
    flux suspend ks "$k" -n "$ns"
    paused=("ks $k" "${paused[@]+"${paused[@]}"}")
    # A zeroscaler HPA would restore a replica after the scale-down; the Kustomization recreates it.
    kubectl -n "$ns" delete hpa -l "kustomize.toolkit.fluxcd.io/name=$k" --ignore-not-found
done
for h in "${hrs[@]+"${hrs[@]}"}"; do
    flux suspend hr "$h" -n "$ns"
    paused=("hr $h" "${paused[@]}")
done
workloads=$(jq -r --argjson h "$hrs_json" --argjson k "$kss_json" "$mine_jq"'
    .items[] | select(.kind != "CronJob") | select(mine) | "\(.kind)/\(.metadata.name) \(.spec.replicas // 1)"' \
    <<<"$controllers")
while read -r obj replicas; do
    [ -n "$obj" ] || continue
    scaled+=("$obj $replicas")
    kubectl -n "$ns" scale "$obj" --replicas=0
done <<<"$workloads"
cronjobs=$(jq -r --argjson h "$hrs_json" --argjson k "$kss_json" "$mine_jq"'
    .items[] | select(.kind == "CronJob") | select(mine) | "cronjob/\(.metadata.name) \(.spec.suspend // false)"' \
    <<<"$controllers")
while read -r cj already; do
    [ -n "$cj" ] || continue
    [ "$already" = "true" ] && continue
    suspended+=("$cj")
    kubectl -n "$ns" patch "$cj" -p '{"spec":{"suspend":true}}'
done <<<"$cronjobs"

# A Job between retries has no pod but will write again, so wait until every Job that mounts a
# claim has finished, whoever owns it; then until no pod mounts one.
until jobs=$(kubectl get jobs -n "$ns" -o json) && open=$(jq -r --argjson c "$claims" \
    '.items[] | select(any(.spec.template.spec.volumes[]?; .persistentVolumeClaim.claimName | IN($c[])))
    | select(any(.status.conditions[]?; (.type == "Complete" or .type == "Failed") and .status == "True") | not)
    | .metadata.name' <<<"$jobs") && [ -z "$open" ]; do tick "unfinished Jobs"; done
until pods=$(kubectl get pods -n "$ns" -o json) && users=$(jq -r --argjson c "$claims" \
    '.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed")
    | select(any(.spec.volumes[]?; .persistentVolumeClaim.claimName | IN($c[]))) | .metadata.name' \
    <<<"$pods") && [ -z "$users" ]; do tick "pods mounting a claim"; done
echo "writers stopped: $(date -u +%FT%TZ)"

rc=0
sources=$(kubectl -n "$ns" get replicationsource -o jsonpath='{range .items[*]}{.spec.sourcePVC} {.metadata.name}{"\n"}{end}')
while read -r claim; do
    [ -n "$claim" ] || continue
    mine=$(awk -v c="$claim" '$1 == c { print $2 }' <<<"$sources")
    if [ -z "$mine" ]; then
        echo "claim $claim: no ReplicationSource (no backup: copy it out instead)"
        rc=1
        continue
    fi
    for rs in $mine; do
        # A run already in progress completes as the manual sync and may predate the stop.
        until [ "$(kubectl -n "$ns" get replicationsource "$rs" \
            -o jsonpath='{.status.conditions[?(@.type=="Synchronizing")].status}')" = "False" ]; do tick "$rs to go idle"; done
        t="retire-$(date +%s)"
        kubectl -n "$ns" patch replicationsource "$rs" --type merge \
            -p "{\"spec\":{\"trigger\":{\"manual\":\"$t\"}}}" >/dev/null
        until [ "$(kubectl -n "$ns" get replicationsource "$rs" -o jsonpath='{.status.lastManualSync}')" = "$t" ]; do
            tick "$rs manual sync"
        done
        line=$(kubectl -n "$ns" get replicationsource "$rs" \
            -o jsonpath='{.metadata.name} {.status.latestMoverStatus.result} {.status.lastSyncTime}')
        echo "claim $claim: $line"
        [ "$(cut -d' ' -f2 <<<"$line")" = "Successful" ] || rc=1
    done
done <<<"$claims_txt"

# Restore before the verdict, so "BACKUP OK" appears only once the app runs again.
trap - EXIT
if ! cleanup; then
    echo "CLEANUP INCOMPLETE: restore the app by hand; do not merge until Flux is resumed." >&2
    exit 1
fi
echo "restored and resumed: ${kss[*]} (HelmReleases: ${hrs[*]:-none})"
if [ "$rc" -ne 0 ]; then
    echo "BACKUP INCOMPLETE: do not merge." >&2
    exit 1
fi
echo "BACKUP OK: merge the archive PR now; writes after the resume are not in the backup."
