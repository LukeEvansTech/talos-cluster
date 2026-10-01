#!/usr/bin/env bash
# Re-runs the claude/renovate-review gate on every open Renovate PR whose gate failed on the Claude
# usage limit (429). The limit is shared with interactive sessions and resets on a schedule the
# status text names, so after the reset the gate approves on a plain re-run.
#
# Usage: renovate-rerun-limited.sh [probe|all]
#   probe (default) re-runs ONE gate: if it 429s again the window has not reset, so stop there.
#   all             re-runs every limited gate.
# One GraphQL read for the scan; each re-run is one REST call.
set -euo pipefail

mode=${1:-probe}
case "$mode" in
probe | all) ;;
*)
    echo "usage: renovate-rerun-limited.sh [probe|all]" >&2
    exit 2
    ;;
esac
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

# Captured first: a failed scan must not read as an empty result through process substitution.
# shellcheck disable=SC2016 # GraphQL variables, not shell expansions
scan=$(gh api graphql -f query='query($o:String!,$n:String!){repository(owner:$o,name:$n){
    pullRequests(states:OPEN,first:100){nodes{number author{login}
        commits(last:1){nodes{commit{status{contexts{context state description targetUrl}}}}}}}}}' \
    -f o="${repo%%/*}" -f n="${repo#*/}" --jq '.data.repository.pullRequests.nodes[]
    | select(.author.login == "renovate")
    | .number as $n
    | .commits.nodes[0].commit.status.contexts[]?
    | select(.context == "claude/renovate-review" and .state == "FAILURE")
    | select(.description | test("usage limit"; "i"))
    | "\($n) \(.targetUrl | capture("runs/(?<id>[0-9]+)").id)"')
limited=()
[ -z "$scan" ] || mapfile -t limited <<<"$scan"

if [ ${#limited[@]} -eq 0 ]; then
    echo "renovate-rerun-limited: no gate is failing on the usage limit"
    exit 0
fi
echo "renovate-rerun-limited: ${#limited[@]} PR(s) limited: $(printf '#%s ' "${limited[@]%% *}")"
[ "$mode" = probe ] && limited=("${limited[0]}")
for entry in "${limited[@]}"; do
    pr=${entry%% *}
    run=${entry#* }
    if gh run rerun "$run" --failed -R "$repo" >/dev/null 2>&1; then
        echo "  #$pr: re-ran gate run $run"
    else
        echo "  #$pr: re-run of $run refused (already running, or too old to re-run)"
    fi
done
if [ "$mode" = probe ]; then
    echo "renovate-rerun-limited: probe started; if #${limited[0]%% *} approves in a few minutes, run with 'all'"
fi
