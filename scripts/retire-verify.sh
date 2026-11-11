#!/usr/bin/env bash
# After an archive PR merges, list what of the app is still live. Prints only names, never values.
# Runbook: docs/docs/operations/retiring-an-app.md, steps 4-6 and "Done when".
# Usage: retire-verify.sh <namespace> <app>
set -euo pipefail

ns=${1:?namespace}
app=${2:?app}
[[ "$app" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "app must be a plain name" >&2
    exit 2
}
m="(^|[/[:space:]])${app}([-[:space:]]|$)"
left=0
report() { # <label> <lines>
    if [ -n "$2" ]; then
        echo "LEFT  $1:"
        awk '{ print "        " $0 }' <<<"$2"
        left=1
    else
        echo "gone  $1"
    fi
}

report "Flux Kustomizations" "$(flux get kustomizations -A --no-header | grep -E "$m" || true)"
report "Flux HelmReleases" "$(flux get helmreleases -A --no-header | grep -E "$m" || true)"
report "objects in $ns" "$(kubectl -n "$ns" get deploy,statefulset,cronjob,pvc,replicationsource,replicationdestination,httproute,externalsecret \
    --no-headers -o name | grep -E "/${app}([-]|$)" || true)"
report "LiteLLM virtual key and PushSecret" "$(kubectl -n ai get litellmvirtualkeys,pushsecrets --no-headers -o name | grep -E "/${app}$" || true)"

# The key list is queried inside the litellm pod, which holds the master key, with a live alias as
# the control so an empty answer from a broken query cannot pass.
control=$(kubectl -n ai get litellmvirtualkeys -o jsonpath='{.items[0].spec.keyAlias}')
counts=$(kubectl -n ai exec deploy/litellm -c litellm -- python3 -c '
import json, os, sys, urllib.request
def n(alias):
    req = urllib.request.Request(
        "http://localhost:4000/key/list?return_full_object=true&size=100&key_alias=" + alias,
        headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    return d.get("total_count", len(d.get("keys", [])))
print(n(sys.argv[1]), n(sys.argv[2]))
' "$app" "$control")
read -r app_keys control_keys <<<"$counts"
if [ "$control_keys" -lt 1 ]; then
    echo "BLIND LiteLLM key list: control alias '$control' not found, so the query proves nothing"
    left=1
elif [ "$app_keys" -gt 0 ]; then
    echo "LEFT  LiteLLM key alias '$app' ($app_keys)"
    left=1
else
    echo "gone  LiteLLM key alias (control '$control' found)"
fi

field="LITELLM_$(tr 'a-z-' 'A-Z_' <<<"$app")_API_KEY"
readers=$(git grep -n "$field" origin/main -- kubernetes || true)
report "readers of 1Password field $field on origin/main" "$readers"
echo "check 1Password by hand: delete field $field from the litellm item once it has no readers"

python3 "$(git rev-parse --show-toplevel)/.github/scripts/check_archived_app_refs.py" || left=1
exit "$left"
