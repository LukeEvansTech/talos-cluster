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

# Flux skips the prune for a Kustomization or HelmRelease deleted while suspended, so resume on
# every exit, failure included; the app runs again until the PR merges.
# shellcheck disable=SC2329 # invoked by the EXIT trap
resume() {
    for k in "${kss[@]}"; do
        flux resume hr "$k" -n "$ns" >/dev/null 2>&1 || echo "WARN: flux resume hr $k failed" >&2
        flux resume ks "$k" -n "$ns" >/dev/null 2>&1 || echo "WARN: flux resume ks $k failed" >&2
    done
    echo "resumed: ${kss[*]}"
}
trap resume EXIT

for k in "${kss[@]}"; do
    flux suspend ks "$k" -n "$ns"
    flux suspend hr "$k" -n "$ns"
    sel="helm.toolkit.fluxcd.io/name=$k"
    kubectl -n "$ns" scale deploy,statefulset -l "$sel" --replicas=0
    for cj in $(kubectl -n "$ns" get cronjob -l "$sel" -o name); do
        kubectl -n "$ns" patch "$cj" -p '{"spec":{"suspend":true}}'
    done
    # A zeroscaler HPA would restore a replica.
    kubectl -n "$ns" delete hpa -l "kustomize.toolkit.fluxcd.io/name=$k" --ignore-not-found
done

# A failed query must not read as "no writers": both commands have to succeed.
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
        kubectl -n "$ns" patch replicationsource "$rs" --type merge -p "{\"spec\":{\"trigger\":{\"manual\":\"$t\"}}}" >/dev/null
        until [ "$(kubectl -n "$ns" get replicationsource "$rs" -o jsonpath='{.status.lastManualSync}')" = "$t" ]; do sleep 10; done
        line=$(kubectl -n "$ns" get replicationsource "$rs" \
            -o jsonpath='{.metadata.name} {.status.latestMoverStatus.result} {.status.lastSyncTime}')
        echo "$line"
        [ "$(cut -d' ' -f2 <<<"$line")" = "Successful" ] || rc=1
    done
done

if [ "$rc" -eq 0 ]; then
    echo "BACKUP OK: merge the archive PR now; writes after the resume are not in the backup."
else
    echo "BACKUP INCOMPLETE: do not merge." >&2
fi
exit "$rc"
