# curatortv

The deterministic half of the weekly Sonarr library cleanup, forked from the
film engine in `kubernetes/apps/media/curator/app/resources/curator/`. Judgement
belongs to `curatortvjudge`. The film engine's readme is the design document for
everything shared: the fail-closed rules, why a zero play count has four
meanings, why the identity join runs from history rows, why every output names
its plan, and why the volatile checks are re-read per deletion. All of that
holds here unchanged. This file covers only what is different about TV.

## Commands

Run from the directory above this one, which is the package root.

```bash
python3 -m curatortv plan --out ./cleanup-plan      # read-only, always safe
python3 -m curatortv execute --plan ./cleanup-plan/plan.json \
    --recommendations ./recommendations.json --out ./result.json --read-only
python3 -m curatortv report --plan ./cleanup-plan/plan.json \
    --result ./result.json --out ./report.md        # read-only, always safe
python3 -m curatortv selftest                       # fixture tests, no network
python3 -m curatortv.provision                      # show decision-tag changes
```

## Exit codes

`execute` exits non-zero for anything that should not read as a successful week,
which is what the staleness alert keys on. The CronJob's shell keeps the first
non-zero code and still sends the digest.

| Code | Meaning                                                           |
| ---- | ----------------------------------------------------------------- |
| 2    | the plan recorded blocking issues                                 |
| 3    | another run holds the execute lock                                |
| 4    | a deletion failed or could not be confirmed                       |
| 5    | refused: `--read-only` in act mode, or a stale or wrong-mode plan |
| 7    | the recommendations were judged against a different plan          |
| 8    | a `keep` tag could not be confirmed on re-read                    |
| 9    | a candidate came back unjudged, or a verdict was rejected         |
| 10   | a deletion was skipped because a safety check could not be read   |

`report` exits 6 when the digest could not be delivered.

## The one deliberate deviation: `keep` is written in a dry run

The film engine writes nothing at all in a dry run. This one adds the `keep`
tag, to the series over the rating bar and to every series the judge verdicts
`keep`, when `TAG_KEEPS_IN_DRY_RUN=true`, even under `--read-only`. Reclaimerr
protects anything tagged `keep`, so a dry run that writes nothing leaves it
blind to every decision this engine has made; and an add-only tag is reversible
by removing it, which a deletion is not.

The live tags are re-read before each addition, and a series carrying
`cleanup-eligible`, `cleanup-dismissed` or `keep-review` is never tagged: those
are a person's decision. Deletion is untouched by the flag and stays impossible
in a dry run or under `--read-only`, exactly as in the film engine.

A judge `keep` is therefore permanent rather than a 180-day spare: once tagged,
the series is an absolute veto until a person removes the tag. The result file
reports both writes, as `keep_tags_added` (rating bar) and `judge_keeps_tagged`. Each
tag is confirmed by re-reading the series; a judge keep whose tag cannot be
confirmed is not recorded as a decision, so it is judged and tagged again next
run, and the run exits non-zero.

## What changes for a series

**The unit is the series.** `series_id` replaces `movie_id` everywhere: the plan,
the recommendations, the ledger keys and the judge prompt. Deleting removes the
whole series with its files and adds an import-list exclusion keyed on TVDB ID.

**Availability is per episode.** Each episode keeps its _earliest_ import, so a
quality upgrade cannot restart a clock. An ended series' window runs from its
first import. A continuing series' window runs from the newest episode's first
import, because a season that arrived last month is not something anybody has
had a chance to watch. The history-coverage check still uses the series' first
import: old seasons were watchable too, and their plays have to be visible.

**Viewing is counted in episodes.** Tautulli episode rows are joined to a series
by `grandparent_rating_key`, the show's own Plex item. Every key sharing any
TVDB, TMDb or IMDb ID with the series counts, since an older key may know it by
only one of them; a key naming a different ID under any scheme is another show.
The title-and-year fallback (used only when no ID matches), retired-key taint and
ambiguity rules are as in the film engine. A person counts as a
viewer once they finish (85%+) one episode; two viewers send the series to a
person, mirroring two completers for a film. `episodes_completed` is the most
distinct episodes any one person has finished.

**A retired show key taints on title alone.** An episode row's `year` is the
episode's, not the premiere's, so a play behind an unresolvable show key taints
every series with that title; only a show year from the row's own show metadata
narrows it. A Plex item that answers with no TVDB, TMDb or IMDb ID taints the
same way, and is asked about again next run rather than cached.

**A person's tag stops a deletion.** The live re-check refuses a series that
gained `cleanup-dismissed`, as well as any veto tag, since the plan.

**New episodes stop a deletion.** Immediately before each delete, the series'
import history and episode-file count are re-read; anything imported after the
plan, or more files than the plan saw, skips the series this week.

**Provenance comes from Kometa tags.** Sonarr here has no import lists. A series
is `feed` when it carries any tag in `FEED_TAGS` (default `kometa`), `requested`
when Seerr has a request for its TVDB ID (or TMDb ID as a fallback), and
`unknown` otherwise. Unknown origin is never authorised for deletion; a person's
`cleanup-eligible` tag authorises it.

**There are no collections.** The film engine's monitored-collection protection,
`siblings_owned` and collection anomaly checks have no TV equivalent and are
gone. Genre `subject_counts` remain.

**Scope.** `upcoming` series are out of scope. `CLEANUP_SCOPE_START` gates on the
added date as before; Sonarr was rebuilt in February 2026, so most added dates
fall then, before the default `2026-03-01`.

## Configuration

As the film engine, with `SONARR_URL` / `SONARR_API_KEY` in place of the Radarr
pair, plus:

| Variable               | Meaning                                                       |
| ---------------------- | ------------------------------------------------------------- |
| `FEED_TAGS`            | comma-separated tag labels proving a feed added a series      |
| `TAG_KEEPS_IN_DRY_RUN` | `true` lets a dry run add the `keep` tag; deletion unaffected |

## Undoing a deletion

1. Re-add the series in Sonarr from the intent record, which holds `tvdbId`,
   `path`, `rootFolderPath`, `qualityProfileId`, `seriesType`, `seasonFolder`,
   `monitored`, per-season monitoring and the tag IDs.
2. Restore the files from the recycle bin and rescan.
3. Remove the import-list exclusion, found by **tvdbId** under
   `/api/v3/importlistexclusion`.
