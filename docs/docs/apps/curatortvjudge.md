# Curatortvjudge

The taste half of the weekly Sonarr library cleanup, in the `media` namespace. It is
[Curatorjudge](curatorjudge.md) pointed at the TV engine: the same three steps (`prompt`, `judge`,
`app`), the same hardening, `backoffLimit: 0`, a judge that never mounts the state volume, and an
executor run with `--read-only` and `CLEANUP_MODE: dry-run`. Read that page for the trust boundary
and failure semantics; this page covers what differs.

## What differs from Curatorjudge

- **Inputs:** reads the plan from the `curatortv` volume and runs `python3 -m curatortv` from the
  `curatortv-code` ConfigMap.
- **Rubric:** `taste.md` defaults to **keep**, where the film rubric defaults to delete. A delete
  needs a nameable, series-specific reason (poorly received and unwatched, a Kometa add that never
  caught on, filler with no standing). Ratings with substantial votes, awards, a well-known
  completed series, or a notable network or creator count as reasons to keep. Size never decides.
  The engine already protects everything over the rating bar, so the judge sees series below it
  or with no trusted rating.
- **Verdicts:** `[{"series_id": …, "verdict": …, "reason": …}]`. Both `delete` and `keep` need a
  reason of at least 12 characters, because a `keep` is written to Sonarr (below).
- **Digest:** same webhook as Curatorjudge; every title starts `TV cleanup` so the two are
  distinguishable on the phone.

## Keep tags are written in a dry run

`TAG_KEEPS_IN_DRY_RUN: "true"` lets the `app` step add the `keep` tag to rating-bar series and to
every series the judge verdicts `keep`, even though the run is a dry run with `--read-only`. This
is the one place the TV pipeline writes where the film pipeline does not, and it exists for
Reclaimerr, which spares anything tagged `keep`: without it, a dry run leaves Reclaimerr blind to
every decision made here.

- It is add-only, so it is undone by removing the tag.
- Each series' live tags are re-read first; a series carrying `cleanup-eligible`,
  `cleanup-dismissed` or `keep-review` is never tagged.
- Deletion is unaffected: still impossible in dry-run mode or under `--read-only`.
- A judge `keep` becomes permanent: `keep` is an absolute veto, so the series is not re-judged
  after the usual 180 days unless a person removes the tag.

The result file reports both writes as `keep_tags_added` and `judge_keeps_tagged`, and the digest
lists them. Each tag is confirmed by re-reading the series: a keep whose tag did not land is not
recorded as a decision, so it is retried next week, and the run exits non-zero.

## Shared credentials

The Claude token reuses the `claude-code-curator` 1Password item (`curatortvjudge-token`), so there
is one expiry to watch, not two. The digest token is `chaski`'s `TRIAGE_WEBHOOK_TOKEN`, as for films.
