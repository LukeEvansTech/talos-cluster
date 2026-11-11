#!/usr/bin/env bash
# Stop an app being retired and take a fresh VolSync sync of every destination, so the backup
# postdates the app's last write. Runbook: docs/docs/operations/retiring-an-app.md, step 1.
# Usage: retire-backup.sh <namespace> <kustomization>...  (every Kustomization in the app's ks.yaml)
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
    for k in "${kss[@]}"; do
        flux resume hr "$k" -n "$ns" >/dev/null || {
            echo "RESUME FAILED: hr $k" >&2
            rc=1
        }
        flux resume ks "$k" -n "$ns" >/dev/null || {
            echo "RESUME FAILED: ks $k" >&2
            rc=1
        }
    done
    return "$rc"
}
# shellcheck disable=SC2329 # invoked by the EXIT trap
on_exit() {
    cleanup || echo "CLEANUP INCOMPLETE: restore the app by hand before merging" >&2
}
trap on_exit EXIT

for k in "${kss[@]}"; do
    flux suspend ks "$k" -n "$ns"
    flux suspend hr "$k" -n "$ns"
    sel="helm.toolkit.fluxcd.io/name=$k"
    # Assigned on their own line so a failed query stops the run instead of reading as "none".
    workloads=$(kubectl -n "$ns" get deploy,statefulset -l "$sel" \
        -o jsonpath='{range .items[*]}{.kind}/{.metadata.name} {.spec.replicas}{"\n"}{end}')
    while read -r obj replicas; do
        [ -n "$obj" ] || continue
        scaled+=("$obj $replicas")
        kubectl -n "$ns" scale "$obj" --replicas=0
    done <<<"$workloads"
    cronjobs=$(kubectl -n "$ns" get cronjob -l "$sel" \
        -o jsonpath='{range .items[*]}cronjob/{.metadata.name} {.spec.suspend}{"\n"}{end}')
    while read -r cj already; do
        [ -n "$cj" ] || continue
        [ "$already" = "true" ] && continue
        suspended+=("$cj")
        kubectl -n "$ns" patch "$cj" -p '{"spec":{"suspend":true}}'
    done <<<"$cronjobs"
    # A zeroscaler HPA would restore a replica; the Kustomization recreates it on resume.
    kubectl -n "$ns" delete hpa -l "kustomize.toolkit.fluxcd.io/name=$k" --ignore-not-found
done

# A Job between retries has no pod but will write again, so wait until every Job its suspended
# CronJobs own has finished, not just until its pods are gone.
owners=$(printf '%s\n' "${suspended[@]+"${suspended[@]}"}" | sed -n 's|^cronjob/||p' | jq -R . | jq -sc .)
until jobs=$(kubectl get jobs -n "$ns" -o json) && open=$(jq -r --argjson o "$owners" \
    '.items[] | select(any(.metadata.ownerReferences[]?; .kind == "CronJob" and (.name | IN($o[]))))
    | select(any(.status.conditions[]?; (.type == "Complete" or .type == "Failed") and .status == "True") | not)
    | .metadata.name' <<<"$jobs") && [ -z "$open" ]; do sleep 10; done

# Pods that still mount a claim are writers: a scaled-down Deployment's terminating pod, a Job's.
for claim in "${kss[@]}"; do
    until pods=$(kubectl get pods -n "$ns" -o json) && users=$(jq -r --arg c "$claim" \
        '.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed")
        | select(any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $c)) | .metadata.name' \
        <<<"$pods") && [ -z "$users" ]; do sleep 10; done
done
echo "writers stopped: $(date -u +%FT%TZ)"

rc=0
for k in "${kss[@]}"; do
    for rs in "$k-nfs" "$k-r2"; do
        if ! kubectl -n "$ns" get replicationsource "$rs" >/dev/null 2>&1; then
            echo "$rs NotFound (no backup: copy the claim out instead)"
            rc=1
            continue
        fi
        # A run already in progress completes as the manual sync and may predate the stop.
        until [ "$(kubectl -n "$ns" get replicationsource "$rs" \
            -o jsonpath='{.status.conditions[?(@.type=="Synchronizing")].status}')" = "False" ]; do sleep 10; done
        t="retire-$(date +%s)"
        kubectl -n "$ns" patch replicationsource "$rs" --type merge \
            -p "{\"spec\":{\"trigger\":{\"manual\":\"$t\"}}}" >/dev/null
        until [ "$(kubectl -n "$ns" get replicationsource "$rs" -o jsonpath='{.status.lastManualSync}')" = "$t" ]; do
            sleep 10
        done
        line=$(kubectl -n "$ns" get replicationsource "$rs" \
            -o jsonpath='{.metadata.name} {.status.latestMoverStatus.result} {.status.lastSyncTime}')
        echo "$line"
        [ "$(cut -d' ' -f2 <<<"$line")" = "Successful" ] || rc=1
    done
done

# Restore before the verdict, so "BACKUP OK" appears only once the app runs again.
trap - EXIT
if ! cleanup; then
    echo "CLEANUP INCOMPLETE: restore the app by hand; do not merge until Flux is resumed." >&2
    exit 1
fi
echo "restored and resumed: ${kss[*]}"
if [ "$rc" -ne 0 ]; then
    echo "BACKUP INCOMPLETE: do not merge." >&2
    exit 1
fi
echo "BACKUP OK: merge the archive PR now; writes after the resume are not in the backup."
