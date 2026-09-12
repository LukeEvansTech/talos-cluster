#!/bin/sh
# Turn the engine's plan into recommendations, using Claude for the judgement.
#
# Runs as an init container so the main container only ever sees a file: if this
# fails, or writes nothing, execute finds no recommendations and does nothing.
# That is the intended failure mode -- a judge that cannot answer must not
# produce deletions.
set -eu

PLAN=/state/cleanup-plan/plan.json
OUT=/state/cleanup-plan/recommendations.json

if [ ! -f "$PLAN" ]; then
    echo "no plan at $PLAN -- has the curator job run? nothing to judge."
    exit 1
fi

# blocks non-empty means the engine refused this run outright (no baseline yet,
# unusable play history, a recycle bin that would make deletion unrecoverable,
# a population that collapsed). Judging anyway would waste a call and invite
# acting on a run that already said no.
if [ "$(jq '.blocks | length' "$PLAN")" != "0" ]; then
    echo "plan recorded blocking issues; not judging:"
    jq -r '.blocks[]' "$PLAN"
    echo '[]' >"$OUT"
    exit 0
fi

COUNT=$(jq '.candidates | length' "$PLAN")
echo "candidates to judge: $COUNT"
if [ "$COUNT" = "0" ]; then
    echo "nothing to judge."
    echo '[]' >"$OUT"
    exit 0
fi

# Only the fields judgement needs. Withholding the rest keeps the prompt small
# and keeps identifiers the model has no use for out of it.
CANDIDATES=$(jq -c '[.candidates[] | {
    movie_id, title, year, imdb_score, imdb_votes, size_gb,
    days_available, play_count, distinct_completers,
    collection, genres, studio, overview
}]' "$PLAN")

{
    cat /prompt/taste.md
    echo
    echo "## Candidates"
    echo
    echo "$CANDIDATES"
} >/tmp/prompt.txt

# --max-turns 1 and every tool denied: this is one structured judgement, not an
# agent. It has no business touching a filesystem or a network.
claude -p \
    --output-format json \
    --max-turns 1 \
    --model "${JUDGE_MODEL:-claude-opus-5}" \
    --disallowed-tools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit" \
    </tmp/prompt.txt >/tmp/claude.json

if [ "$(jq -r '.is_error' /tmp/claude.json)" = "true" ]; then
    echo "claude reported an error:"
    jq -r '.result // .subtype' /tmp/claude.json
    exit 1
fi

echo "judged in $(jq -r '.duration_ms' /tmp/claude.json)ms, cost \$$(jq -r '.total_cost_usd' /tmp/claude.json)"

# The model was told to emit a bare array; strip a code fence if it adds one
# anyway, then require the result to parse as an array before it is written.
# execute validates every id against the candidate set regardless, but a
# malformed file should fail here where the message is obvious.
jq -r '.result' /tmp/claude.json |
    sed -e 's/^```json//' -e 's/^```//' -e 's/```$//' |
    jq 'if type == "array" then . else ("expected a JSON array, got \(type)" | halt_error(1)) end' \
        >"$OUT"

echo "wrote $(jq 'length' "$OUT") recommendations to $OUT"
